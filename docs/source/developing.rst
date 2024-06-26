Developing
==========

Running the Tests
-----------------

After building the project for develoment, go to the project directory 
and use pytest:

.. code-block:: bash

        pytest enspara/test/ -m 'not mpi'

This command runs all the tests, except those that require MPI. To include
those tests, simply remove the ``-m 'not mpi'``. However, these may or may not
function unless you run the tests under MPI.

To run the tests under MPI, you'll run something that looks like:

.. code-block:: bash

   mpiexec -n 2 pytest enspara/test/ -m 'mpi'

This will run only the mpi tests with an MPI swarm of two nodes. Obviously not
all pathology will be caught by just two MPI nodes, but it's a start!
