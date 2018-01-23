
# Author: Gregory R. Bowman <gregoryrbowman@gmail.com>
# Contributors:
# Copyright (c) 2016, Washington University in St. Louis
# All rights reserved.
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential

from __future__ import print_function, division, absolute_import

import logging

import numpy as np

from sklearn.utils import check_random_state

from ..util.log import timed
from .. import mpi

from . import util

logger = logging.getLogger(__name__)


def kmedoids(X, distance_method, n_clusters, n_iters=5):
    """K-Medoids clustering.

    K-Medoids is a clustering algorithm similar to the k-means algorithm
    but the center of each cluster is required to actually be an
    observation in the input data.

    Parameters
    ----------
    X : array-like, shape=(n_observations, n_features, *)
        Data to cluster. The user is responsible for pre-partitioning
        this data across nodes.
    distance_method : callable
        Function that takes a parameter like `X` and a single frame
        of `X` (_i.e._ X.shape[1:]).
    cluster_center_inds : list, [(owner_rank, world_index), ...]
        A list of the locations of center indices in terms of the rank
        of the node that owns them and the index within that world.
    assignments : ndarray, shape=(X.shape[0],)
        Array indicating the assignment of each frame in `X` to a
        cluster center.
    n_iters : int, default=5
        Number of rounds of new proposed centers to run.

    Returns
    -------
    result : ClusterResult
        Subclass of NamedTuple containing assignments, distances,
        and center indices for this function.

    References
    ----------
    .. [1]  Chodera, J. D., Singhal, N., Pande, V. S., Dill, K. A. & Swope, W. C. Automatic discovery of metastable states for the construction of Markov models of macromolecular conformational dynamics. J. Chem. Phys. 126, 155101 (2007).
    """

    distance_method = util._get_distance_method(distance_method)

    n_frames = len(X)

    # for short lists, np.random.random_integers sometimes forgets to assign
    # something to each cluster. This will simply repeat the assignments if
    # that is the case.
    cluster_center_inds = np.array([])
    while len(np.unique(cluster_center_inds)) < n_clusters:
        cluster_center_inds = np.random.randint(0, n_frames, n_clusters)

    assignments, distances = util.assign_to_nearest_center(
        X, X[cluster_center_inds], distance_method)
    cluster_center_inds = util.find_cluster_centers(assignments, distances)

    for i in range(n_iters):
        cluster_center_inds, distances, assignments = _kmedoids_pam_update(
            X, distance_method, cluster_center_inds, assignments,
            distances)
        logger.info("KMedoids update %s", i)

    return util.ClusterResult(
        center_indices=cluster_center_inds,
        assignments=assignments,
        distances=distances,
        centers=X[cluster_center_inds])


def _msq(x):
    return mpi.ops.mean(np.square(x))


def _kmedoids_pam_update(
        X, metric, medoid_inds, assignments,
        distances, cost=_msq, random_state=None):
    """Compute a kmedoids update using Partitioning Around Medoids (PAM)

    PAM iteratively proposes a new cluster center from among the points
    assigned to a cluster center, recomputes the cost function, and
    accepts the proposal iff cost goes down. Its time complexity is
    O(k*n*i), where k is the number of centers, n is the size of the data
    and i is the number of iterations (i.e. each invocation of this
    function costs O(kn).)

    Parameters
    ----------
    X : array-like, shape=(n_observations, n_features, *)
        Data to cluster. The user is responsible for pre-partitioning
        this data across nodes.
    metric : callable
        Function that takes a parameter like `X` and a single frame
        of `X` (_i.e._ X.shape[1:]).
    medoid_inds : list, [(rank, index), ...] if MPI or [index, ...]
        A list of the locations of center indices in terms of the rank
        of the node that owns them and the index within that world.
    assignments : ndarray, shape=(X.shape[0],)
        Array indicating the assignment of each frame in `X` to a
        cluster center.
    distances : ndarray, shape=(X.shape[0],)
        Array giving the distance between this observation/frame and the
        relevant cluster center.
    cost : callable, default='meansquare'
        Function computing the cost of a particular clustering. Should
        take a vector of distances and returning a number. This value is
        minimzed.
    random_state : numpy.RandomState
        RandomState object used to indentify new centers.

    Returns
    -------
    updated_cluster_center_inds : list, [(owner_rank, world_index), ...]
        A list of the locations of center indices in terms of the rank
        of the node that owns them and the index within that world.
    updated_assignments : ndarray, shape=(traj.shape[0],)
        Array indicating the assignment of each frame in `traj` to a
        cluster center.
    updated_distances : ndarray, shape=(traj.shape[0],)
        Array giving the distance between this observation/frame and the
        relevant cluster center.
    """

    assert np.issubdtype(type(assignments[0]), np.integer)
    assert len(assignments) == len(X)
    assert len(distances) == len(X)
    random_state = check_random_state(random_state)

    # first we build a list of the actual coordinates of the cluster centers
    # this list will be updated as we go; this is primarily because we want
    # to limit the amount of communication that happens when we're running
    # MPI mode.
    medoid_coords = []
    if hasattr(medoid_inds[0], '__len__'):
        assert len(medoid_inds[0]) == 2
        for center_idx, (rank, frame_idx) in enumerate(medoid_inds):
            assert rank < mpi.MPI_SIZE
            new_center = mpi.ops.distribute_frame(
                data=X, owner_rank=rank, world_index=frame_idx)
            medoid_coords.append(new_center)
    else:
        medoid_coords = [X[i] for i in medoid_inds]

    acceptances = 0
    for cid in range(len(medoid_inds)):
        state_inds = np.where(assignments == cid)[0]

        # first, we propose a new center. This works a bit differently
        # if we're running with MPI, because we want to make a choice
        # that uniformly distributed across any node.

        # TODO: make it impossible to choose the current center
        if hasattr(medoid_inds[0], '__len__'):
            assert len(medoid_inds[0]) == 2
            r, i = mpi.ops.np_choice(state_inds, random_state)
            proposed_center = mpi.ops.distribute_frame(
                data=X, owner_rank=r, world_index=i)
            proposed_center_ind = (r, i)
        else:
            proposed_center_ind = random_state.choice(state_inds)
            print(len(state_inds))
            proposed_center = X[proposed_center_ind]

        logger.debug("Proposed new medoid (%s -> %s) for k=%s",
                     medoid_inds[cid], proposed_center_ind, cid)

        # In the PAM method, once we have a new center, we recompute
        # the distance from this center to every point. Depending on if
        # the distance goes up or down, and which old center it was
        # assigned to (this or other), we update distances and assignents.
        new_ctr_dist = metric(X, proposed_center)

        new_dist = np.zeros_like(distances) - 1
        new_assig = np.zeros_like(assignments) - 1

        # if the new center decreases the distance below whatever it is
        # to its current medoid (cid or not cid), assign it to cid
        dst_dn = (distances > new_ctr_dist)
        new_assig[dst_dn] = cid
        new_dist[dst_dn] = new_ctr_dist[dst_dn]

        # if the new center increases the distance, we have to think
        # harder. If it's assigned to some other medoid, we just copy
        # the old assignments into the new assignments array.
        dst_up_assig_other = (distances <= new_ctr_dist) & (assignments != cid)
        new_assig[dst_up_assig_other] = assignments[dst_up_assig_other]
        new_dist[dst_up_assig_other] = distances[dst_up_assig_other]

        # if the new center increases the distance to cid and it was
        # previously assigned to cid, then we have to compute the
        # distance to _all_ other medoids :(
        dst_up_assig_this = (distances <= new_ctr_dist) & (assignments == cid)

        new_medoids = medoid_coords.copy()
        new_medoids[cid] = proposed_center

        with timed("Recomputed nearest medoid for {n} points in %.2f sec.".\
                   format(n=np.count_nonzero(dst_up_assig_this)),
                   logger.debug):
            ambig_assigs, ambig_dists = util.assign_to_nearest_center(
                X[dst_up_assig_this], new_medoids, metric)

        new_assig[dst_up_assig_this] = ambig_assigs
        new_dist[dst_up_assig_this] = ambig_dists

        # every element of new_dist and new_assig should have been touched
        assert np.all(new_assig >= 0)
        assert np.all(new_dist >= 0)

        old_cost = cost(distances)
        new_cost = cost(new_dist)

        if new_cost < old_cost:
            logger.debug(
                "Accepted proposed center for k=%s: cost %.5f -> %.5f).",
                cid, old_cost, new_cost)
            distances, assignments = new_dist, new_assig
            medoid_coords = new_medoids
            medoid_inds[cid] = proposed_center_ind
            acceptances += 1
        else:
            logger.debug(
                "Rejected proposed center for k=%s: cost %.5f -> %.5f).",
                cid, old_cost, new_cost)

    logger.info("Kmedoid sweep reduced cost to %.7f (%.2f%% acceptance)",
                min(old_cost, new_cost), acceptances/len(medoid_inds)*100)

    return medoid_inds, distances, assignments
