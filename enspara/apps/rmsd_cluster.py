import sys
import argparse
import os

import numpy as np
import mdtraj as md
from mdtraj import io

from enspara.cluster import KHybrid
from enspara.util import load_as_concatenated


def process_command_line(argv):
    '''Parse the command line and do a first-pass on processing them into a
    format appropriate for the rest of the script.'''

    parser = argparse.ArgumentParser(formatter_class=argparse.
                                     ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--trajectories', required=True, nargs="+",
        help="The aligned xtc files to cluster.")
    parser.add_argument(
        '--topology', required=True,
        help="The topology file for the trajectories.")
    parser.add_argument(
        '--algorithm', required=True, choices=["khybrid"],
        help="The clustering algorithm to use.")
    parser.add_argument(
        '--atoms', default='name C or name O or name CA or name N or name CB',
        help="The atoms from the trajectories (using MDTraj atom-selection"
             "syntax) to cluster based upon.")
    parser.add_argument(
        '--rmsd-cutoff', required=True, type=float,
        help="The RMSD cutoff (in nm) to determine cluster size.")
    parser.add_argument(
        '--processes', default=1, type=int,
        help="Number processes to use for loading and clustering.")
    parser.add_argument(
        '--output', default='',
        help="The output path forresults (distances, assignments, centers).")
    parser.add_argument(
        '--output-tag', default='',
        help="An optional extra string prepended to output filenames (useful"
             "for giving this choice of parameters a name to separate it from"
             "other clusterings or proteins.")

    args = parser.parse_args(argv[1:])

    return args


def rmsd_hack(trj, ref, **kwargs):
    pivot = int(len(trj)/2)

    trj1 = trj[0:pivot]
    trj2 = trj[pivot:]

    rmsds = np.zeros(len(trj))

    rmsds[0:pivot] = md.rmsd(trj1, ref, **kwargs)
    rmsds[pivot:]  = md.rmsd(trj2, ref, **kwargs)

    return rmsds


def main(argv=None):
    '''Run the driver script for this module. This code only runs if we're
    being run as a script. Otherwise, it's silent and just exposes methods.'''
    args = process_command_line(argv)

    top = md.load(args.topology).top

    # loads a giant trajectory in parallel into a single numpy array.
    print("Loading", len(args.trajectories), "trajectories using",
          args.processes, "processes")

    lengths, xyz = load_as_concatenated(
        args.trajectories, top=top, processes=args.processes)

    print("Loading finished. Beginning clustering.")

    clustering = KHybrid(
        metric=rmsd_hack,
        cluster_radius=args.rmsd_cutoff)

    # md.rmsd requires an md.Trajectory object, so wrap `xyz` in
    # the topology.
    clustering.fit(md.Trajectory(xyz=xyz, topology=top))

    # partition the concatenated arrays in result_ into trj-length arrays
    result = clustering.result_.partition(lengths)

    path_stub = os.path.join(
        args.output, '-'.join([args.output_tag, args.algorithm,
                               str(args.rmsd_cutoff)]))

    io.saveh(path_stub+'-distances.h5', result.distances)
    io.saveh(path_stub+'-assignments.h5', result.assignments)
    # io.saveh(path_stub+'-center-indices.h5', result.center_indices)

    centers = md.join([md.load_frame(args.trajectories[t], index=i, top=top)
                       for t, i in result.center_indices])
    centers.save_hdf5(path_stub+'-centers.h5')

    print("Clustered", sum(lengths), "frames into", len(centers),
          "clusters in", clustering.runtime_, "seconds.")

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
