import pytest

from dolfin import mpi_comm_world, MPI, set_log_level, DEBUG, INFO
from muflon.common.parameters import mpset, MuflonParameterSet

import os

def test_mpset():
    #set_log_level(DEBUG)

    # Print parameters and their values
    #mpset.show()

    # Check that assignment out of range raises
    # FIXME: dolfin/parameter/Parameter.cpp is broken.
    #        It doesn't raise when assigning a value out of range;
    #        see 921c56cee4f50f016a07f49a5e90f6627c7317a6
    # with pytest.raises(RuntimeError):
    #     mpset["discretization"]["N"] = 1
    # with pytest.raises(RuntimeError):
    #     mpset["model"]["mobility"]["beta"] = 2.0
    with pytest.raises(RuntimeError):
        mpset["model"]["mobility"]["m"] = 0.0

    # Try to add parameter
    mpset.add("foo", "bar")
    assert mpset["foo"] == "bar"

    # Try direct access to a parameter
    mpset["foo"] = "bar_"
    assert mpset["foo"] == "bar_"

    # Try to write parameters to a file
    comm = mpi_comm_world()
    tempdir = "/tmp/pytest-of-fenics"
    fname = tempdir+"/foo.xml"
    mpset.write(comm, fname)
    if MPI.rank(comm) == 0:
        assert os.path.isfile(fname)
    MPI.barrier(comm) # wait until the file is written

    # Change back value of parameter 'foo'
    mpset["foo"] = "bar"
    assert mpset["foo"] == "bar"

    # Try to read parameters back
    mpset.read(fname)
    assert mpset["foo"] == "bar_"
    MPI.barrier(comm) # wait until each process finishes reading
    if MPI.rank(comm) == 0:
        os.remove(fname)
    del fname

    # Check that every other call points to the same object
    assert id(MuflonParameterSet()) == id(mpset)

    # Cleanup
    set_log_level(INFO)
    mpset.refresh()
