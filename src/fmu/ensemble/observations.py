# -*- coding: utf-8 -*-
"""
Observations support and related calculations
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import math
import datetime
from collections import OrderedDict

import yaml
import pandas as pd
import dateutil

from .etc import Interaction
from .realization import ScratchRealization
from .ensemble import ScratchEnsemble
from .ensembleset import EnsembleSet
from .virtualrealization import VirtualRealization
from .virtualensemble import VirtualEnsemble

xfmu = Interaction()
logger = xfmu.functionlogger(__name__)


class Observations(object):
    """Represents a set of observations and the ability to
    compare realizations and ensembles to the observations

    The primary data structure is a dictionary holding actual
    observations, this can typically be loaded from a yaml-file

    Key functionality is to be able to compute mismatch pr
    observation and presenting the computed data as a Pandas
    Dataframe. If run on ensembles, every row will be tagged
    by which realization index the data was computed for.

    An observation unit is a concept for the observation and points to
    something we define as a "single" observation. It can be one value
    for one datatype at a specific date, but in the case of Eclipse
    summary vector, it can also be a time-series. Mismatches will
    be computed pr. observation unit.

    Pay attentiont to mismatch versus misfit. Here, mismatch is used
    for individual observation units, while misfit is used as single
    number for whole realizations.

    Important: Using time-series as observations is not recommended in
    assisted history match. Pick individual uncorrelated data points
    at relevant points in time instead.

    The type of observations supported must follow the datatypes that
    the realizations and ensemble objects are able to internalize.

    """

    # Discussion points:
    # * Should mismatch calculation happen in this function
    #   with ensembles/realizations input or the other way around?
    # * Should it be possible to represent the observations
    #   themselves in a dataframe, or does the dict suffice?
    #   (each observation unit should be representable as
    #   a dict, and then it is mergeable in Pandas)

    def __init__(self, observations):
        """Initialize an observation object with observations
        from file or from an incoming dictionary structure

        Observations will be checked for validity, and
        incorrect observations (wrong format, unsupported etc.)
        will be removed. Empty observation list is allowed, and
        will typically end in empty result dataframes

        Args:
            observations: dict with observation structure or string
                with path to a yaml file.
        """
        self.observations = dict()

        if isinstance(observations, str):
            with open(observations) as yamlfile:
                self.observations = yaml.full_load(yamlfile)
        elif isinstance(observations, dict):
            self.observations = observations
        else:
            raise ValueError("Unsupported object for observations")

        # Remove unsupported observations
        # Identify and warn about errors in observation syntax (dates etc)
        self._clean_observations()

        logger.info("Initialized observation with obstypes %s", str(self.keys()))
        for obskey in self.keys():
            # (fixme: this string does not make sense)
            logger.info(" %s: ", str(len(self.observations[obskey])))

    def __getitem__(self, someobject):
        """Pick objects from the observations dict"""
        return self.observations[someobject]

    def mismatch(self, ens_or_real):
        """Compute the mismatch from the current observation set
        to the incoming ensemble or realization.

        In the case of an ensemble, it will calculate individually
        for every realization, and aggregate the results.

        Returns:
            dataframe with REAL (only if ensemble), OBSKEY, DATE,
                L1, L2. One row for every observation unit.
        """
        # For ensembles, we should in the future be able to loop
        # over realizations in a multiprocessing fashion
        if isinstance(ens_or_real, EnsembleSet):
            mismatches = {}
            for ensname, ens in ens_or_real._ensembles.items():
                logger.info("Calculating mismatch for ensemble %s", ensname)
                for realidx, real in ens._realizations.items():
                    logger.info("Calculating mismatch for realization %s", str(realidx))
                    mismatches[(ensname, realidx)] = self._realization_mismatch(real)
                    mismatches[(ensname, realidx)]["REAL"] = realidx
                    mismatches[(ensname, realidx)]["ENSEMBLE"] = ensname
            return pd.concat(mismatches, axis=0, ignore_index=True)
        elif isinstance(ens_or_real, ScratchEnsemble):
            mismatches = {}
            for realidx, real in ens_or_real._realizations.items():
                mismatches[realidx] = self._realization_mismatch(real)
                mismatches[realidx]["REAL"] = realidx
            return pd.concat(mismatches, axis=0, ignore_index=True, sort=False)
        elif isinstance(ens_or_real, VirtualEnsemble):
            mismatches = {}
            for realidx in ens_or_real.realindices:
                mismatches[realidx] = self._realization_mismatch(
                    ens_or_real.get_realization(realidx)
                )
                mismatches[realidx]["REAL"] = realidx
            return pd.concat(mismatches, axis=0, ignore_index=True, sort=False)
        elif isinstance(ens_or_real, (ScratchRealization, VirtualRealization)):
            return self._realization_mismatch(ens_or_real)
        elif isinstance(ens_or_real, EnsembleSet):
            pass
        else:
            raise ValueError("Unsupported object for mismatch calculation")
        return None

    def load_smry(self, realization, smryvector, time_index="yearly", smryerror=None):
        """Add an observation unit from a VirtualRealization or
        ScratchRealization, being a specific summaryvector, picking
        values with the specified time resolution.

        This can be used to compare similarity between realization, by
        viewing simulated results as "observations". A use case is
        to rank all realizations in an ensemble for the similarity to
        a certain mean profile, f.ex. FOPT.

        The result of the function is a observation unit added to
        the smry observations, with values at every date.

        Arguments:
            realization: ScratchRealization or VirtualRealization containing
                data for constructing the virtual observation
            smryvector: string with a name of a specific summary vector
                to be used
            time_index: string with timeresolution, typically 'yearly'
                or 'monthly'. The Realization must already have data
                loaded at this time resolution.
            smryerror: float, constant value to be used as the measurement
                error for every date.
        """

        # We can only assume VirtualRealizations coming in, not
        # ScratchRealizations. VirtualRealization currenly lack a
        # get_smry() API that will interpolate its known data.
        # That means we have to guess which dataset to load for
        # smry data, and we cannot support arbitrary time indices
        data_name = "unsmry--" + str(time_index)

        # A ValueError will be thrown if the realization does not have
        # the smry data loaded, and a KeyError if incorrect summary vector name
        dataseries = realization.get_df(data_name).set_index("DATE")[smryvector]

        # Modify the observation object (self)
        if "smry" not in self.observations.keys():
            self.observations["smry"] = []  # Empty list

        # Construct a virtual observation with observation units
        # at every timestep:
        virtobs = {}
        virtobs["key"] = smryvector
        virtobs["comment"] = "Virtual observation unit constructed from " + str(
            realization
        )
        virtobs["observations"] = []
        for date, value in dataseries.iteritems():
            virtobs["observations"].append(
                {"value": value, "error": smryerror, "date": date}
            )
        self.observations["smry"].append(virtobs)

    def __len__(self):

        """Return the number of observation units present"""
        # This is not correctly implemented yet..
        return len(self.observations.keys())

    @property
    def empty(self):
        """Decide if the observation set is empty

        An empty observation set is has zero observation
        unit count"""
        return not self.__len__()

    def keys(self):
        """Return a list of observation units present.

        This list might change into a dataframe in the future,
        but calling len() on its results should always return
        the number of observation units."""
        return self.observations.keys()

    def _realization_mismatch(self, real):
        """Compute the mismatch from the current loaded
        observations to a realization.

        Supports both ScratchRealizations and
        VirtualRealizations

        The returned dataframe contains the columns:
            * OBSTYPE - category/type of the observation
            * OBSKEY - name of the observation key
            * DATE - only where relevant.
            * OBSINDEX - where an enumeration is relevant
            * MISMATCH - signed difference between value and result
            * L1 - absolute difference
            * L2 - absolute difference squared
            * SIGN - True if positive difference
            * SIMVALUE - the simulated value, not for smryh
            * OBSVALUE - the observed value, not for smryh
        One row for every observation unit.

        Args:
            real : ScratchRealization or VirtualRealization
        Returns:
            dataframe: One row per observation unit with
                mismatch data
        """
        # mismatch_df = pd.DataFrame(columns=['OBSTYPE', 'OBSKEY',
        #     'DATE', 'OBSINDEX', 'MISMATCH', 'L1', 'L2', 'SIGN'])
        mismatches = []
        for obstype in self.observations.keys():
            for obsunit in self.observations[obstype]:  # (list)
                if obstype == "txt":
                    try:
                        sim_value = real.get_df(obsunit["localpath"])[obsunit["key"]]
                    except KeyError:
                        logger.warning(
                            "%s in %s not found, ignored",
                            obsunit["key"],
                            obsunit["localpath"],
                        )
                        continue
                    except ValueError:
                        logger.warning("%s not found, ignored", obsunit["localpath"])
                        continue
                    mismatch = float(sim_value - obsunit["value"])
                    measerror = 1
                    sign = (mismatch > 0) - (mismatch < 0)
                    mismatches.append(
                        dict(
                            OBSTYPE=obstype,
                            OBSKEY=str(obsunit["localpath"])
                            + "/"
                            + str(obsunit["key"]),
                            MISMATCH=mismatch,
                            L1=abs(mismatch),
                            L2=abs(mismatch) ** 2,
                            SIMVALUE=sim_value,
                            OBSVALUE=obsunit["value"],
                            MEASERROR=measerror,
                            SIGN=sign,
                        )
                    )
                if obstype == "scalar":
                    try:
                        sim_value = real.get_df(obsunit["key"])
                    except ValueError:
                        logger.warning(
                            "No data found for scalar: %s, ignored", obsunit["key"]
                        )
                        continue
                    mismatch = float(sim_value - obsunit["value"])
                    measerror = 1
                    sign = (mismatch > 0) - (mismatch < 0)
                    mismatches.append(
                        dict(
                            OBSTYPE=obstype,
                            OBSKEY=str(obsunit["key"]),
                            MISMATCH=mismatch,
                            L1=abs(mismatch),
                            SIMVALUE=sim_value,
                            OBSVALUE=obsunit["value"],
                            MEASERROR=measerror,
                            L2=abs(mismatch) ** 2,
                            SIGN=sign,
                        )
                    )
                if obstype == "smryh":
                    if "time_index" in obsunit:
                        sim_hist = real.get_smry(
                            time_index=obsunit["time_index"],
                            column_keys=[obsunit["key"], obsunit["histvec"]],
                        )
                    else:
                        sim_hist = real.get_smry(
                            column_keys=[obsunit["key"], obsunit["histvec"]]
                            # (let get_smry() determine the possible time_index)
                        )
                    # If empty df returned, we don't have the data for this:
                    if sim_hist.empty:
                        logger.warning(
                            "No data found for smryh: %s and %s, ignored.",
                            obsunit["key"],
                            obsunit["histvec"],
                        )
                        continue
                    sim_hist["mismatch"] = (
                        sim_hist[obsunit["key"]] - sim_hist[obsunit["histvec"]]
                    )
                    measerror = 1
                    mismatches.append(
                        dict(
                            OBSTYPE="smryh",
                            OBSKEY=obsunit["key"],
                            MISMATCH=sim_hist["mismatch"].sum(),
                            MEASERROR=measerror,
                            L1=sim_hist["mismatch"].abs().sum(),
                            L2=math.sqrt((sim_hist["mismatch"] ** 2).sum()),
                        )
                    )
                if obstype == "smry":
                    # For 'smry', there is a list of
                    # observations (indexed by date)
                    for unit in obsunit["observations"]:
                        try:
                            sim_value = real.get_smry(
                                time_index=[unit["date"]], column_keys=obsunit["key"]
                            )[obsunit["key"]].values[0]
                        except KeyError:
                            logger.warning(
                                "No data found for smry: %s at %s, ignored.",
                                obsunit["key"],
                                str(unit["date"]),
                            )
                            continue
                        mismatch = float(sim_value - unit["value"])
                        sign = (mismatch > 0) - (mismatch < 0)
                        mismatches.append(
                            dict(
                                OBSTYPE="smry",
                                OBSKEY=obsunit["key"],
                                DATE=unit["date"],
                                MEASERROR=unit["error"],
                                MISMATCH=mismatch,
                                OBSVALUE=unit["value"],
                                SIMVALUE=sim_value,
                                L1=abs(mismatch),
                                L2=abs(mismatch) ** 2,
                                SIGN=sign,
                            )
                        )
        return pd.DataFrame(mismatches)

    def _realization_misfit(self, real, defaulterrors=False, corr=None):
        """The misfit value for the observation set

        Ref: https://wiki.statoil.no/wiki/index.php/RP_HM/Observations#Misfit_function

        Args:
            real : a ScratchRealization or a VirtualRealization
            defaulterrors: (boolean) If set to True, zero measurement errors
                will be set to 1.
            corr : correlation or weigthing matrix (numpy matrix).
                If a list or numpy vector is supplied, it is interpreted
                as a diagonal matrix. If omitted, the identity matrix is used

        Returns:
            float : the misfit value for the observation set and realization
        """  # noqa
        if corr:
            raise NotImplementedError(
                "correlations in misfit " + "calculation is not supported"
            )
        mismatch = self._realization_mismatch(real)

        zeroerrors = mismatch["MEASERROR"] < 1e-7
        if defaulterrors:
            mismatch[zeroerrors]["MEASERROR"] = 1
        else:
            if zeroerrors.any():
                print(mismatch[zeroerrors])
                raise ValueError(
                    "Zero measurement error in observation set"
                    + ". can't be used to calculate misfit"
                )
        if "MISFIT" not in mismatch.columns:
            mismatch["MISFIT"] = mismatch["L2"] / (mismatch["MEASERROR"] ** 2)

        return mismatch["MISFIT"].sum()

    def _clean_observations(self):
        """Verify integrity of observations, remove
        observation units that cannot be used.

        Will log warnings about things that are removed.

        Returns number of usable observation units.

        Ensure that dates are parsed into datetime.date objects.
        """
        supported_categories = ["smry", "smryh", "txt", "scalar", "rft"]

        # Check top level keys in observations dict:
        for key in list(self.observations):
            if key not in supported_categories:
                self.observations.pop(key)
                logger.error("Observation category %s not supported", key)
                continue
            if not isinstance(self.observations[key], list):
                logger.error(
                    "Observation category %s did not contain a " + "list, but %s",
                    key,
                    type(self.observations[key]),
                )
                self.observations.pop(key)

        # Check smry observations for validity
        if "smry" in self.observations.keys():
            # We already know that observations['smry'] is a list
            # Each list element must be a dict with
            # the mandatory keys 'key' and 'observation'
            smryunits = self.observations["smry"]
            for unit in smryunits:
                if not isinstance(unit, (dict, OrderedDict)):
                    logger.warning(
                        "Observation units must be dicts, deleting: %s", str(unit)
                    )
                    del smryunits[smryunits.index(unit)]
                    continue
                if not ("key" in unit and "observations" in unit):
                    logger.warning(
                        (
                            "Observation unit must contain key and",
                            "observations, deleting: %s",
                        ),
                        str(unit),
                    )
                    del smryunits[smryunits.index(unit)]
                    continue
                # Check if strings need to be parsed as dates:
                for observation in unit["observations"]:
                    if isinstance(observation["date"], str):
                        observation["date"] = dateutil.parser.isoparse(
                            observation["date"]
                        ).date()
                    if not isinstance(observation["date"], datetime.date):
                        logger.error("Date not understood %s", str(observation["date"]))
                        continue
            # If everything is deleted from 'smry', delete it
            if not smryunits:
                del self.observations["smry"]

    def to_ert2observations(self):
        """Convert the observation set to an observation
        file for use with Ert 2.x.

        Returns: multiline string
        """
        raise NotImplementedError

    def __repr__(self):
        """Return a representation of the object

        The representation is a YAML string
        that can be used to reinstatiate the
        object
        """
        return self.to_yaml()

    def to_yaml(self):
        """Convert the current observations to YAML format

        Returns:
            string : Multiline YAML string.
        """
        return yaml.dump(self.observations)

    def to_disk(self, filename):
        """Write the current observation object to disk

        In YAML-format. If a new observation object
        is instantiated from the outputted filename, it
        should yield identical results in mismatch
        calculation.

        Directory structure will be created if not existing.
        Existing file will be overwritten.

        Arguments:
            filename - string with path and filename to
                be written to"""
        if not isinstance(filename, str):
            raise ValueError("Filename must be a string")
        dirname = os.path.dirname(filename)
        if not os.path.exists(dirname) and dirname:
            os.makedirs(dirname)
        with open(filename, "w") as fhandle:
            fhandle.write(self.to_yaml())
