# -*- coding: utf-8 -*-
"""Testing fmu-ensemble, EnsembleSet class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import glob
import shutil
import pandas as pd

import pytest

from fmu.ensemble import etc
from fmu.ensemble import ScratchEnsemble, EnsembleSet

try:
    skip_fmu_tools = False
    from fmu.tools import volumetrics
except ImportError:
    skip_fmu_tools = True

fmux = etc.Interaction()
logger = fmux.basiclogger(__name__, level="WARNING")

if not fmux.testsetup():
    raise SystemExit()


def test_ensembleset_reek001(tmp="TMP"):
    """Test import of a stripped 5 realization ensemble,
    manually doubled to two identical ensembles
    """

    if "__file__" in globals():
        # Easen up copying test code into interactive sessions
        testdir = os.path.dirname(os.path.abspath(__file__))
    else:
        testdir = os.path.abspath(".")
    ensdir = os.path.join(testdir, "data/testensemble-reek001/")

    # Copy iter-0 to iter-1, creating an identical ensemble
    # we can load for testing.
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        if os.path.exists(realizationdir + "/iter-1"):
            if os.path.islink(realizationdir + "/iter-1"):
                os.remove(realizationdir + "/iter-1")
            else:
                shutil.rmtree(realizationdir + "/iter-1")
        os.symlink(realizationdir + "/iter-0", realizationdir + "/iter-1")

    iter0 = ScratchEnsemble("iter-0", ensdir + "/realization-*/iter-0")
    iter1 = ScratchEnsemble("iter-1", ensdir + "/realization-*/iter-1")

    ensset = EnsembleSet("reek001", [iter0, iter1])
    assert len(ensset) == 2
    assert len(ensset["iter-0"].get_df("STATUS")) == 250
    assert len(ensset["iter-1"].get_df("STATUS")) == 250

    # Try adding the same object over again
    try:
        ensset.add_ensemble(iter0)
    except ValueError:
        pass
    assert len(ensset) == 2  # Unchanged!

    # Initializing nothing, we get warning about the missing name
    noname = EnsembleSet()
    assert noname.name  # not None
    assert isinstance(noname.name, str)  # And it should be a string

    # Initialize starting from empty ensemble
    ensset2 = EnsembleSet("reek001", [])
    assert ensset2.name == "reek001"
    ensset2.add_ensemble(iter0)
    ensset2.add_ensemble(iter1)
    assert len(ensset2) == 2

    # Check that we can skip the empty list:
    ensset2x = EnsembleSet("reek001")
    ensset2x.add_ensemble(iter0)
    ensset2x.add_ensemble(iter1)
    assert len(ensset2x) == 2

    # Initialize directly from path with globbing:
    ensset3 = EnsembleSet("reek001direct", [])
    assert ensset3.name == "reek001direct"
    ensset3.add_ensembles_frompath(ensdir)
    assert len(ensset3) == 2

    # Alternative globbing:
    ensset4 = EnsembleSet("reek001direct2", frompath=ensdir)
    assert len(ensset4) == 2

    # Testing aggregation of parameters
    paramsdf = ensset3.parameters
    if not os.path.exists(tmp):
        os.mkdir(tmp)
    paramsdf.to_csv(os.path.join(tmp, "enssetparams.csv"), index=False)
    assert isinstance(paramsdf, pd.DataFrame)
    assert len(ensset3.parameters) == 10
    assert len(ensset3.parameters.columns) == 27
    assert "ENSEMBLE" in ensset3.parameters.columns
    assert "REAL" in ensset3.parameters.columns

    outputs = ensset3.load_txt("outputs.txt")
    assert "NPV" in outputs.columns

    # Test Eclipse summary handling:
    assert len(ensset3.get_smry_dates(freq="report")) == 641
    assert len(ensset3.get_smry_dates(freq="monthly")) == 37
    assert len(ensset3.load_smry(column_keys=["FOPT"], time_index="yearly")) == 50
    monthly = ensset3.load_smry(column_keys=["F*"], time_index="monthly")
    assert monthly.columns[0] == "ENSEMBLE"
    assert monthly.columns[1] == "REAL"
    assert monthly.columns[2] == "DATE"

    # Eclipse well names
    assert len(ensset3.get_wellnames("OP*")) == 5
    assert len(ensset3.get_wellnames("WI*")) == 3
    assert len(ensset3.get_wellnames("")) == 0
    assert len(ensset3.get_wellnames()) == 8

    # Check that we can retrieve cached versions
    assert len(ensset3.get_df("unsmry--monthly")) == 380
    assert len(ensset3.get_df("unsmry--yearly")) == 50
    monthly.to_csv(os.path.join(tmp, "ensset-monthly.csv"), index=False)

    with pytest.raises(ValueError):
        ensset3.get_df("unsmry--weekly")

    # Check errors when we ask for stupid stuff
    with pytest.raises(ValueError):
        ensset3.load_csv("bogus.csv")
    with pytest.raises(ValueError):
        ensset3.get_df("bogus.csv")

    # Check get_smry()
    smry = ensset3.get_smry(
        time_index="yearly", column_keys=["FWCT", "FGOR"], end_date="2002-02-01"
    )
    assert "ENSEMBLE" in smry
    assert "REAL" in smry
    assert len(smry["ENSEMBLE"].unique()) == 2
    assert len(smry["REAL"].unique()) == 5
    assert "FWCT" in smry
    assert "FGOR" in smry
    assert "DATE" in smry
    assert len(smry) == 40

    # Eclipse well names list
    assert len(ensset3.get_wellnames("OP*")) == 5
    assert len(ensset3.get_wellnames(None)) == 8
    assert len(ensset3.get_wellnames()) == 8
    assert len(ensset3.get_wellnames("")) == 0
    assert len(ensset3.get_wellnames(["OP*", "WI*"])) == 8

    # Test aggregation of csv files:
    vol_df = ensset3.load_csv("share/results/volumes/" + "simulator_volume_fipnum.csv")
    assert "REAL" in vol_df
    assert "ENSEMBLE" in vol_df
    assert len(vol_df["REAL"].unique()) == 3
    assert len(vol_df["ENSEMBLE"].unique()) == 2
    assert len(ensset3.keys()) == 7

    # Test scalar imports:
    ensset3.load_scalar("npv.txt")
    npv = ensset3.get_df("npv.txt")
    assert "ENSEMBLE" in npv
    assert "REAL" in npv
    assert "npv.txt" in npv
    assert len(npv) == 10
    # Scalar import with forced numerics:
    ensset3.load_scalar("npv.txt", convert_numeric=True, force_reread=True)
    npv = ensset3.get_df("npv.txt")
    assert len(npv) == 8

    predel_len = len(ensset3.keys())
    ensset3.drop("parameters.txt")
    assert len(ensset3.keys()) == predel_len - 1

    # Test callback functionality, that we can convert rms
    # volumetrics in each realization. First we need a
    # wrapper which is able to work on ScratchRealizations.
    def rms_vol2df(kwargs):
        fullpath = os.path.join(kwargs["realization"].runpath(), kwargs["filename"])
        # The supplied callback should not fail too easy.
        if os.path.exists(fullpath):
            return volumetrics.rmsvolumetrics_txt2df(fullpath)
        else:
            return pd.DataFrame()

    if not skip_fmu_tools:
        rmsvols_df = ensset3.apply(
            rms_vol2df, filename="share/results/volumes/" + "geogrid_vol_oil_1.txt"
        )
        assert rmsvols_df["STOIIP_OIL"].sum() > 0
        assert len(rmsvols_df["REAL"].unique()) == 4
        assert len(rmsvols_df["ENSEMBLE"].unique()) == 2

        # Test that we can dump to disk as well and load from csv:
        ensset3.apply(
            rms_vol2df,
            filename="share/results/volumes/" + "geogrid_vol_oil_1.txt",
            localpath="share/results/volumes/geogrid--oil.csv",
            dumptodisk=True,
        )
        geogrid_oil = ensset3.load_csv("share/results/volumes/geogrid--oil.csv")
        assert len(geogrid_oil["REAL"].unique()) == 4
        assert len(geogrid_oil["ENSEMBLE"].unique()) == 2
        # Clean up what we just dumped:
        for real_dir in glob.glob(ensdir + "/realization-*"):
            csvfile = real_dir + "/iter-0/share/results/volumes/geogrid--oil.csv"
            if os.path.exists(csvfile):
                os.remove(csvfile)

    # Initialize differently, using only the root path containing
    # realization-*
    ensset4 = EnsembleSet("foo", frompath=ensdir)
    assert len(ensset4) == 2
    assert isinstance(ensset4["iter-0"], ScratchEnsemble)
    assert isinstance(ensset4["iter-1"], ScratchEnsemble)

    # Delete the symlink and leftover from apply-testing when we are done.
    for real_dir in glob.glob(ensdir + "/realization-*"):
        if not skip_fmu_tools:
            csvfile = real_dir + "/iter-0/share/results/volumes/geogrid--oil.csv"
            if os.path.exists(csvfile):
                os.remove(csvfile)
        if os.path.exists(real_dir + "/iter-1"):
            os.remove(real_dir + "/iter-1")


def test_pred_dir():
    """Test import of a stripped 5 realization ensemble,
    manually doubled to two identical ensembles,
    plus a prediction ensemble
    """

    if "__file__" in globals():
        # Easen up copying test code into interactive sessions
        testdir = os.path.dirname(os.path.abspath(__file__))
    else:
        testdir = os.path.abspath(".")
    ensdir = os.path.join(testdir, "data/testensemble-reek001/")

    # Copy iter-0 to iter-1, creating an identical ensemble
    # we can load for testing. Delete in case it exists
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        if os.path.exists(realizationdir + "/iter-1"):
            os.remove(realizationdir + "/iter-1")
        os.symlink(realizationdir + "/iter-0", realizationdir + "/iter-1")
        if os.path.exists(realizationdir + "/pred-dg3"):
            os.remove(realizationdir + "/pred-dg3")
        os.symlink(realizationdir + "/iter-0", realizationdir + "/pred-dg3")

    # Initialize differently, using only the root path containing
    # realization-*. The frompath argument does not support
    # anything but iter-* naming convention for ensembles (yet?)
    ensset = EnsembleSet("foo", frompath=ensdir)
    assert len(ensset) == 2
    assert isinstance(ensset["iter-0"], ScratchEnsemble)
    assert isinstance(ensset["iter-1"], ScratchEnsemble)

    # We need to be more explicit to include the pred-dg3 directory:
    pred_ens = ScratchEnsemble("pred-dg3", ensdir + "realization-*/pred-dg3")
    ensset.add_ensemble(pred_ens)
    assert isinstance(ensset["pred-dg3"], ScratchEnsemble)
    assert len(ensset) == 3

    # Check the flagging in aggregated data:
    yearlysum = ensset.load_smry(time_index="yearly")
    assert "ENSEMBLE" in yearlysum.columns

    ens_list = list(yearlysum["ENSEMBLE"].unique())
    assert len(ens_list) == 3
    assert "pred-dg3" in ens_list
    assert "iter-0" in ens_list
    assert "iter-1" in ens_list

    # Try to add a new ensemble with a similar name to an existing:
    foo_ens = ScratchEnsemble("pred-dg3", ensdir + "realization-*/iter-1")
    with pytest.raises(ValueError):
        ensset.add_ensemble(foo_ens)
    assert len(ensset) == 3

    # Delete the symlinks when we are done.
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        os.remove(realizationdir + "/iter-1")
        os.remove(realizationdir + "/pred-dg3")


def test_mangling_data():
    """Test import of a stripped 5 realization ensemble,
    manually doubled to two identical ensembles,
    and then with some data removed
    """

    if "__file__" in globals():
        # Easen up copying test code into interactive sessions
        testdir = os.path.dirname(os.path.abspath(__file__))
    else:
        testdir = os.path.abspath(".")
    ensdir = os.path.join(testdir, "data/testensemble-reek001/")

    # Copy iter-0 to iter-1, creating an identical ensemble<
    # we can load for testing. Delete in case it exists
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        if os.path.exists(realizationdir + "/iter-1"):
            if os.path.islink(realizationdir + "/iter-1"):
                os.remove(realizationdir + "/iter-1")
            else:
                shutil.rmtree(realizationdir + "/iter-1")
        # Symlink each file/dir individually (so we can remove some)
        os.mkdir(realizationdir + "/iter-1")
        for realizationcomponent in glob.glob(realizationdir + "/iter-0/*"):
            if ("parameters.txt" not in realizationcomponent) and (
                "outputs.txt" not in realizationcomponent
            ):
                os.symlink(
                    realizationcomponent,
                    realizationcomponent.replace("iter-0", "iter-1"),
                )

    # Trigger warnings:
    assert not EnsembleSet()  # warning given
    assert not EnsembleSet(["bargh"])  # warning given
    assert not EnsembleSet("bar")  # No warning, just empty
    EnsembleSet("foobar", frompath="foobarth")  # trigger warning

    ensset = EnsembleSet("foo", frompath=ensdir)
    assert len(ensset) == 2
    assert isinstance(ensset["iter-0"], ScratchEnsemble)
    assert isinstance(ensset["iter-1"], ScratchEnsemble)

    assert "parameters.txt" in ensset.keys()

    # We should only have parameters in iter-0
    params = ensset.get_df("parameters.txt")
    assert len(params) == 5
    assert params["ENSEMBLE"].unique() == "iter-0"

    ensset.load_txt("outputs.txt")
    assert "outputs.txt" in ensset.keys()
    assert len(ensset.get_df("outputs.txt")) == 4

    # When it does not exist in any of the ensembles, we
    # should error
    with pytest.raises(ValueError):
        ensset.get_df("foobar")

    # Delete the symlinks when we are done.
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        shutil.rmtree(realizationdir + "/iter-1")


def test_filestructures(tmp="TMP"):
    """Generate filepath structures that we want to be able to initialize
    as ensemblesets.

    This function generatate dummy data
    """
    if "__file__" in globals():
        # Easen up copying test code into interactive sessions
        testdir = os.path.dirname(os.path.abspath(__file__))
    else:
        testdir = os.path.abspath(".")
    ensdir = os.path.join(testdir, tmp, "data/dummycase/")
    if os.path.exists(ensdir):
        shutil.rmtree(ensdir)
    os.makedirs(ensdir)
    no_reals = 5
    no_iters = 4
    for real in range(no_reals):
        for iterr in range(no_iters):  # 'iter' is a builtin..
            runpath1 = os.path.join(ensdir, "iter_" + str(iterr), "real_" + str(real))
            runpath2 = os.path.join(
                ensdir, "real-" + str(real), "iteration" + str(iterr)
            )
            os.makedirs(runpath1)
            os.makedirs(runpath2)
            open(os.path.join(runpath1, "parameters.txt"), "w").write(
                "REALTIMESITER " + str(real * iterr) + "\n"
            )
            open(os.path.join(runpath1, "parameters.txt"), "w").write(
                "REALTIMESITERX2 " + str(real * iterr * 2) + "\n"
            )

    # Initializing from this ensemble root should give nothing,
    # we do not recognize this iter_*/real_* by default
    assert not EnsembleSet("dummytest1", frompath=ensdir)

    # Try to initialize providing the path to be globbed,
    # should still not work because the naming is non-standard:
    assert not EnsembleSet("dummytest2", frompath=ensdir + "iter_*/real_*")
    # If we also provide regexpes, we should be able to:
    dummy = EnsembleSet(
        "dummytest3",
        frompath=ensdir + "iter_*/real_*",
        realidxregexp=re.compile(r"real_(\d+)"),
        iterregexp=r"iter_(\d+)",
    )
    # (regexpes can also be supplied as strings)

    assert len(dummy) == no_iters
    assert len(dummy[dummy.ensemblenames[0]]) == no_reals
    # Ensemble name should be set depending on the iterregexp we supply:
    assert len(dummy.ensemblenames[0]) == len("X")
    for ens_name in dummy.ensemblenames:
        print(dummy[ens_name])
    # Also test if we can compile the regexp automatically, and
    # supply a string instead.
    dummy2 = EnsembleSet(
        "dummytest4",
        frompath=ensdir + "iter_*/real_*",
        realidxregexp="real_(\d+)",
        iterregexp=re.compile(r"(iter_\d+)"),
    )
    # Different regexp for iter, so we get different ensemble names:
    assert len(dummy2.ensemblenames[0]) == len("iter-X")

    dummy3 = EnsembleSet(
        "dummytest5",
        frompath=ensdir + "real-*/iteration*",
        realidxregexp=re.compile(r"real-(\d+)"),
        iterregexp=re.compile(r"iteration(\d+)"),
    )
    assert len(dummy3) == no_iters
    assert len(dummy3[dummy3.ensemblenames[0]]) == no_reals

    # Difficult question whether this code should fail hard
    # or be forgiving for the "erroneous" (ambigous) user input
    dummy6 = EnsembleSet(
        "dummytest6",
        frompath=ensdir + "real-*/iteration*",
        realidxregexp=re.compile(r"real-(\d+)"),
    )
    # Only one ensemble is distingushed because we did not tell
    # the code how the ensembles are named:
    assert len(dummy6) == 1
    # There are 20 realizations in the file structure, but
    # only 5 unique realization indices. We get back an ensemble
    # with 5 members, but exactly which is not defined (or tested)
    assert len(dummy6[dummy6.ensemblenames[0]]) == 5


def test_ertrunpathfile(tmp="TMP"):
    """Initialize an ensemble set from an ERT runpath file

    ERT runpath files look like:
        <rownumber> <path> <case> <iter>
    where rownumber is an integer, path is a string,
    case is usually integer but
    potentially a string? and iter is a integer."""

    if "__file__" in globals():
        # Easen up copying test code into interactive sessions
        testdir = os.path.dirname(os.path.abspath(__file__))
    else:
        testdir = os.path.abspath(".")

    ensdir = os.path.join(testdir, "data/testensemble-reek001/")
    # Copy iter-0 to iter-1, creating an identical ensemble<
    # we can load for testing. Delete in case it exists
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        if os.path.exists(realizationdir + "/iter-1"):
            if os.path.islink(realizationdir + "/iter-1"):
                os.remove(realizationdir + "/iter-1")
            else:
                shutil.rmtree(realizationdir + "/iter-1")
        # Symlink each file/dir individually (so we can remove some)
        os.mkdir(realizationdir + "/iter-1")
        for realizationcomponent in glob.glob(realizationdir + "/iter-0/*"):
            if ("parameters.txt" not in realizationcomponent) and (
                "outputs.txt" not in realizationcomponent
            ):
                os.symlink(
                    realizationcomponent,
                    realizationcomponent.replace("iter-0", "iter-1"),
                )

    # Also construct an artificial ert runpathfile with iter-0 and iter-1,
    # by modifying a copy of the runpath for iter-0

    iter0runpath = open(testdir + "/data/ert-runpath-file", "r").readlines()

    if not os.path.exists(tmp):
        os.mkdir(tmp)

    enssetrunpathfile = open(tmp + "/ensset-runpath-file", "w")
    print(iter0runpath)
    enssetrunpathfile.write("".join(iter0runpath))
    for line in iter0runpath:
        (real, path, eclname, iter) = line.split()
        enssetrunpathfile.write(real + " ")  # CHECK THIS!
        # Could the first column just be the line number?
        # Iterate on the ERT official doc when determined.
        enssetrunpathfile.write(path.replace("iter-0", "iter-1") + " ")
        enssetrunpathfile.write(eclname + " ")
        enssetrunpathfile.write("001" + "\n")
    enssetrunpathfile.close()

    ensset = EnsembleSet("ensfromrunpath", runpathfile=tmp + "/ensset-runpath-file")
    assert len(ensset) == 2
    assert len(ensset["iter-0"]) == 5
    assert len(ensset["iter-1"]) == 5

    # Delete the symlinks when we are done.
    for realizationdir in glob.glob(ensdir + "/realization-*"):
        shutil.rmtree(realizationdir + "/iter-1")
