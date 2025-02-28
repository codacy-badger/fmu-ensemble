# -*- coding: utf-8 -*-
"""Module for the ScratchRealization class

A realization is a set of results from one subsurface model
realization. A realization can be either defined from
its output files from the FMU run on the file system,
it can be computed from other realizations, or it can be
an archived realization.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import copy
import glob
import json
from datetime import datetime, date, time
import dateutil

import numpy
import pandas as pd

import ecl.summary
from ecl.eclfile import EclFile
from ecl.grid import EclGrid
from ecl import EclFileFlagEnum

from .etc import Interaction
from .virtualrealization import VirtualRealization
from .realizationcombination import RealizationCombination

fmux = Interaction()
logger = fmux.basiclogger(__name__)


class ScratchRealization(object):
    r"""A representation of results still present on disk

    ScratchRealizations point to the filesystem for their
    contents.

    A realization must at least contain a STATUS file.
    Additionally, jobs.json and parameters.txt will be attempted
    loaded by default.

    The realization is defined by the pointers to the filesystem.
    When asked for, this object will return data from the
    filesystem (or from cache if already computed).

    The files dataframe is the central filesystem pointer
    repository for the object. It will at least contain
    the columns
    * FULLPATH absolute path to a file
    * FILETYPE filename extension (after last dot)
    * LOCALPATH relative filename inside realization diretory
    * BASENAME filename only. No path. Includes extension

    This dataframe is available as a read-only property from the object

    Args:
        path (str): absolute or relative path to a directory
            containing a realizations files.
        realidxregexp: a compiled regular expression which
            is used to determine the realization index (integer)
            from the path. First match is the index.
            Default: realization-(\d+)
            Only needs to match path components.
            If a string is supplied, it will be attempted
            compiled into a regular expression.
        index: int, the realization index to be used, will
            override anything else.
        autodiscovery: boolean, whether the realization should try to
            auto-discover certain data (UNSMRY files in standard location)
    """

    def __init__(self, path, realidxregexp=None, index=None, autodiscovery=True):
        self._origpath = os.path.abspath(path)
        self.index = None
        self._autodiscovery = autodiscovery

        if not realidxregexp:
            realidxregexp = re.compile(r"realization-(\d+)")
        # Try to compile the regexp on behalf of the user.
        if isinstance(realidxregexp, str):
            realidxregexp = re.compile(realidxregexp)
        if isinstance(realidxregexp, str):
            raise ValueError("Supplied realidxregexp not valid")

        self.files = pd.DataFrame(
            columns=["FULLPATH", "FILETYPE", "LOCALPATH", "BASENAME"]
        )
        self._eclsum = None  # Placeholder for caching
        self._eclsum_include_restart = None  # Flag for cached object

        # The datastore for internalized data. Dictionary
        # indexed by filenames (local to the realization).
        # values in the dictionary can be either dicts or dataframes
        self.data = {}
        self._eclinit = None
        self._eclunrst = None
        self._eclgrid = None
        self._ecldata = None
        self._actnum = None

        abspath = os.path.abspath(path)

        if index is None:
            for path_comp in reversed(os.path.abspath(path).split(os.path.sep)):
                realidxmatch = re.match(realidxregexp, path_comp)
                if realidxmatch:
                    self.index = int(realidxmatch.group(1))
                    break
            else:
                logger.warning(
                    "Could not determine realization "
                    + "index for %s, "
                    + "this cannot be inserted in an Ensemble",
                    abspath,
                )
                logger.warning("Maybe you need to use index=<someinteger>")
                self.index = None
        else:
            self.index = int(index)

        # Now look for some common files, but don't require any
        if os.path.exists(os.path.join(abspath, "STATUS")):
            filerow = {
                "LOCALPATH": "STATUS",
                "FILETYPE": "STATUS",
                "FULLPATH": os.path.join(abspath, "STATUS"),
                "BASENAME": "STATUS",
            }
            self.files = self.files.append(filerow, ignore_index=True)
            self.load_status()
        else:
            logger.warning("No STATUS file, %s", abspath)

        if os.path.exists(os.path.join(abspath, "jobs.json")):
            filerow = {
                "LOCALPATH": "jobs.json",
                "FILETYPE": "json",
                "FULLPATH": os.path.join(abspath, "jobs.json"),
                "BASENAME": "jobs.json",
            }
            self.files = self.files.append(filerow, ignore_index=True)

        if os.path.exists(os.path.join(abspath, "OK")):
            self.load_scalar("OK")

        if os.path.exists(os.path.join(abspath, "parameters.txt")):
            self.load_txt("parameters.txt")

        logger.info("Initialized %s", abspath)

    def runpath(self):
        """Return the runpath ("root") of the realization

        Returns:
            str with a filesystem path which at least existeda
                at time of object initialization.
        """
        return self._origpath

    def to_virtual(self, name=None, deepcopy=True):
        """Convert the current ScratchRealization object
        to a VirtualRealization

        Args:
            description: string, used as label
            deepcopy: boolean. Set to true if you want to continue
               to manipulate the ScratchRealization object
               afterwards without affecting the virtual realization.
               Defaults to True. False will give faster execution.
        """
        if not name:
            name = self._origpath
        if deepcopy:
            return VirtualRealization(name, copy.deepcopy(self.data))
        return VirtualRealization(name, self.data)

    def load_file(self, localpath, fformat, convert_numeric=True, force_reread=False):
        """
        Parse and internalize files from disk.

        Several file formats are supported:
        * txt (one key-value pair pr. line)
        * csv
        * scalar (one number or one string in the first line)
        """
        if fformat == "txt":
            self.load_txt(localpath, convert_numeric, force_reread)
        elif fformat == "csv":
            self.load_csv(localpath, convert_numeric, force_reread)
        elif fformat == "scalar":
            self.load_scalar(localpath, convert_numeric, force_reread)
        else:
            raise ValueError("Unsupported file format %s" % fformat)

    def load_scalar(
        self,
        localpath,
        convert_numeric=False,
        force_reread=False,
        comment=None,
        skip_blank_lines=True,
        skipinitialspace=True,
    ):
        """Parse a single value from a file.

        The value can be a string or a number.

        Empty files are treated as existing, with an empty string as
        the value, different from non-existing files.

        pandas.read_table() is used to parse the contents, the args
        'comment', 'skip_blank_lines', and 'skipinitialspace' is passed on
        to that function.

        Args:
            localpath: path to the file, local to the realization
            convert_numeric: If True, non-numerical content will be thrown away
            force_reread: Reread the data from disk.
        Returns:
            the value read from the file.
        """
        fullpath = os.path.abspath(os.path.join(self._origpath, localpath))
        if not os.path.exists(fullpath):
            raise IOError("File not found: " + fullpath)
        else:
            if fullpath in self.files["FULLPATH"].values and not force_reread:
                # Return cached version
                return self.data[localpath]
            elif fullpath not in self.files["FULLPATH"].values:
                filerow = {
                    "LOCALPATH": localpath,
                    "FILETYPE": localpath.split(".")[-1],
                    "FULLPATH": fullpath,
                    "BASENAME": os.path.split(localpath)[-1],
                }
                self.files = self.files.append(filerow, ignore_index=True)
            try:
                value = pd.read_csv(
                    fullpath,
                    header=None,
                    sep="DONOTSEPARATEANYTHING *%magic%*",
                    engine="python",
                    skip_blank_lines=skip_blank_lines,
                    skipinitialspace=skipinitialspace,
                    comment=comment,
                ).iloc[0, 0]
            except pd.errors.EmptyDataError:
                value = ""
            if convert_numeric:
                value = parse_number(value)
                if not isinstance(value, str):
                    self.data[localpath] = value
                else:
                    # In case we are re-reading, we must
                    # ensure there is no value present now:
                    if localpath in self.data:
                        del self.data[localpath]
            else:
                self.data[localpath] = value
            return value

    def load_txt(self, localpath, convert_numeric=True, force_reread=False):
        """Parse a txt file with
        <key> <value>
        in each line.

        The txt file will be internalized in a dict and will be
        stored if the object is archived. Recommended file
        extension is 'txt'.

        Common usage is internalization of parameters.txt which
        happens by default, but this can be used for all txt files.

        The parsed data is returned as a dict. At the ensemble level
        the same function returns a dataframe.

        There is no get'er for the constructed data, access the
        class variable keyvaluedata directly, or rerun this function.
        (except for parameters.txt, for which there is a property
        called 'parameters')

        Values with spaces are not supported, this is similar
        to ERT's CSV_EXPORT1. Remainder string will be ignored silently.

        Args:
            localpath: path local the realization to the txt file
            convert_numeric: defaults to True, will try to parse
                all values as integers, if not, then floats, and
                strings as the last resort.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.

        Returns:
            dict with the parsed values. Values will be returned as
                integers, floats or strings. If convert_numeric
                is False, all values are strings.
        """
        fullpath = os.path.abspath(os.path.join(self._origpath, localpath))
        if not os.path.exists(fullpath):
            raise IOError("File not found: " + fullpath)
        else:
            if fullpath in self.files["FULLPATH"].values and not force_reread:
                # Return cached version
                return self.data[localpath]
            elif fullpath not in self.files["FULLPATH"].values:
                filerow = {
                    "LOCALPATH": localpath,
                    "FILETYPE": localpath.split(".")[-1],
                    "FULLPATH": fullpath,
                    "BASENAME": os.path.split(localpath)[-1],
                }
                self.files = self.files.append(filerow, ignore_index=True)
            try:
                keyvalues = pd.read_csv(
                    fullpath,
                    sep=r"\s+",
                    index_col=0,
                    dtype=str,
                    usecols=[0, 1],
                    header=None,
                )[1].to_dict()
            except pd.errors.EmptyDataError:
                keyvalues = {}
            if convert_numeric:
                for key in keyvalues:
                    keyvalues[key] = parse_number(keyvalues[key])
            self.data[localpath] = keyvalues
            return keyvalues

    def load_csv(self, localpath, convert_numeric=True, force_reread=False):
        """Parse a CSV file as a DataFrame

        Data will be stored as a DataFrame for later
        access or storage.

        Filename is relative to realization root.

        Args:
            localpath: path local the realization to the txt file
            convert_numeric: defaults to True, will try to parse
                all values as integers, if not, then floats, and
                strings as the last resort.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.

        Returns:
            dataframe: The CSV file loaded. Empty dataframe
                if file is not present.
        """
        fullpath = os.path.abspath(os.path.join(self._origpath, localpath))
        if not os.path.exists(fullpath):
            raise IOError("File not found: " + fullpath)
        else:
            # Look for cached version
            if localpath in self.data and not force_reread:
                return self.data[localpath]
            # Check the file store, append if not there
            if localpath not in self.files["LOCALPATH"].values:
                filerow = {
                    "LOCALPATH": localpath,
                    "FILETYPE": localpath.split(".")[-1],
                    "FULLPATH": fullpath,
                    "BASENAME": os.path.split(localpath)[-1],
                }
                self.files = self.files.append(filerow, ignore_index=True)
            try:
                if convert_numeric:
                    # Trust that Pandas will determine sensible datatypes
                    # faster than the convert_numeric() function
                    dtype = None
                else:
                    dtype = str
                dframe = pd.read_csv(fullpath, dtype=dtype)
            except pd.errors.EmptyDataError:
                dframe = None  # or empty dataframe?

            # Store parsed data:
            self.data[localpath] = dframe
            return dframe

    def load_status(self):
        """Collects the contents of the STATUS files and return
        as a dataframe, with information from jobs.json added if
        available.

        Each row in the dataframe is a finished FORWARD_MODEL
        The STATUS files are parsed and information is extracted.
        Job duration is calculated, but jobs above 24 hours
        get incorrect durations.

        Returns:
            A dataframe with information from the STATUS files.
            Each row represents one job in one of the realizations.
        """
        statusfile = os.path.join(self._origpath, "STATUS")
        if not os.path.exists(statusfile):
            # This should not happen as long as __init__ requires STATUS
            # to be present.
            return pd.DataFrame()  # will be empty
        errorcolumns = ["error" + str(x) for x in range(0, 10)]
        status = pd.read_csv(
            statusfile,
            sep=r"\s+",
            skiprows=1,
            header=None,
            names=["FORWARD_MODEL", "colon", "STARTTIME", "dots", "ENDTIME"]
            + errorcolumns,
            dtype=str,
            engine="python",
            error_bad_lines=False,
            warn_bad_lines=True,
        )

        # dtype str messes up a little bit, pre-Pandas 0.24.1 gives 'None' as
        # a string where data is missing.
        status.replace("None", "", inplace=True)
        # While Pandas 0.24.1 will insert proper Null values in those cells,
        # we fill them with the empty string for the rest of this code to work
        status.fillna("", inplace=True)
        # It should be ok to have both of these statements running, but the
        # replace() is probably superfluous when pandas 0.23 is gone.

        errorjobs = status[errorcolumns[0]] != ""

        # Merge any error strings:
        status.loc[errorjobs, "errorstring"] = (
            status.loc[errorjobs, errorcolumns]
            .astype(str)
            .apply(" ".join, axis=1)
            .apply(str.strip)
        )
        status.drop(errorcolumns, axis=1, inplace=True)

        # Delete potential unwanted row
        status = status[~((status.FORWARD_MODEL == "LSF") & (status.colon == "JOBID:"))]

        if status.empty:
            logger.warning("No parseable data in STATUS")
            self.data["STATUS"] = status
            return status

        status = status.reset_index().drop("colon", axis=1).drop("dots", axis=1)

        # Index the jobs, this makes it possible to match with jobs.json:
        status.insert(0, "JOBINDEX", status.index.astype(int))
        status = status.drop("index", axis=1)
        # Calculate duration. Only Python 3.6 has time.fromisoformat().
        # Warning: Unpandaic code..
        durations = []
        for _, jobrow in status.iterrows():
            if not jobrow["ENDTIME"]:  # A job that is not finished.
                durations.append(numpy.nan)
            else:
                hms = list(map(int, jobrow["STARTTIME"].split(":")))
                start = datetime.combine(
                    date.today(), time(hour=hms[0], minute=hms[1], second=hms[2])
                )
                hms = list(map(int, jobrow["ENDTIME"].split(":")))
                end = datetime.combine(
                    date.today(), time(hour=hms[0], minute=hms[1], second=hms[2])
                )
                # This works also when we have crossed 00:00:00.
                # Jobs > 24 h will be wrong.
                durations.append((end - start).seconds)
        status["DURATION"] = durations

        # Augment data from jobs.json if that file is available:
        jsonfilename = os.path.join(self._origpath, "jobs.json")
        if jsonfilename and os.path.exists(jsonfilename):
            try:
                jobsinfo = json.load(open(jsonfilename))
                jobsinfodf = pd.DataFrame(jobsinfo["jobList"])
                jobsinfodf["JOBINDEX"] = jobsinfodf.index.astype(int)
                # Outer merge means that we will also have jobs from
                # jobs.json that has not started (failed or perhaps
                # the jobs are still running on the cluster)
                status = status.merge(jobsinfodf, how="outer", on="JOBINDEX")
            except ValueError:
                logger.warning("Parsing file %s failed, skipping", jsonfilename)
        status.sort_values(["JOBINDEX"], ascending=True, inplace=True)
        self.data["STATUS"] = status
        return status

    def apply(self, callback, **kwargs):
        """Callback functionality

        A function handle can be supplied which will be executed on
        this realization. The function supplied *must* return
        a Pandas DataFrame. The function can accept an additional
        kwargs dictionary with extra information. Special keys
        in the kwargs data are 'realization', which will hold
        the current realization object. The key 'localpath' is
        also reserved for the use inside this apply(), as it
        is used for the name of the internalized data.

        If the key 'dumptofile' is a boolean and set to True,
        the resulting dataframe is also attempted written
        to disk using the supplied 'localpath'.

        Args:
            **kwargs: dict which is supplied to the callbacked function,
            in which the key 'localpath' also points the the name
            used for data internalization.
        """

        if not kwargs:
            kwargs = {}
        if "realization" in kwargs:
            raise ValueError("Never supply realization= to apply()")
        kwargs["realization"] = self

        # Allow for calling functions which cannot take any
        # arguments:
        try:
            result = callback(kwargs)
        except TypeError:
            result = callback()

        if not isinstance(result, pd.DataFrame):
            raise ValueError(
                "Returned value from applied " + "function must be a dataframe"
            )

        # Only internalize if 'localpath' is given
        if "localpath" in kwargs:
            self.data[kwargs["localpath"]] = result

        if "dumptodisk" in kwargs and kwargs["dumptodisk"]:
            if not kwargs["localpath"]:
                raise ValueError(
                    "localpath must be supplied when" + "dumptodisk is used"
                )
            fullpath = os.path.join(self.runpath(), kwargs["localpath"])
            if not os.path.exists(os.path.dirname(fullpath)):
                os.makedirs(os.path.dirname(fullpath))
            if os.path.exists(fullpath):
                os.unlink(fullpath)
            logger.info("Writing result of function call to %s", fullpath)
            result.to_csv(fullpath, index=False)
        return result

    def __getitem__(self, localpath):
        """Direct access to the realization data structure

        Calls get_df(localpath).
        """
        return self.get_df(localpath)

    def __delitem__(self, localpath):
        """Deletes components in the internal datastore.

        Silently ignores data that is not found.

        Args:
            localpath: string, fully qualified name of key
                (no shorthand as for get_df())
        """
        if localpath in self.keys():
            del self.data[localpath]

    def keys(self):
        """Access the keys of the internal data structure
        """
        return self.data.keys()

    def get_df(self, localpath):
        """Access the internal datastore which contains dataframes or dicts
        or scalars.

        Shorthand is allowed, if the fully qualified localpath is
            'share/results/volumes/simulator_volume_fipnum.csv'
        then you can also get this dataframe returned with these alternatives:
         * simulator_volume_fipnum
         * simulator_volume_fipnum.csv
         * share/results/volumes/simulator_volume_fipnum

        but only as long as there is no ambiguity. In case of ambiguity, a
        ValueError will be raised.

        Args:
            localpath: the idenfier of the data requested

        Returns:
            dataframe or dictionary

        Raises:
            ValueError if data is not found.
        """
        if localpath in self.data.keys():
            return self.data[localpath]
        fullpath = self.shortcut2path(localpath)
        if fullpath in self.data.keys():
            return self.data[self.shortcut2path(localpath)]
        # KeyError would also be valid or better here.
        raise ValueError("Could not find {}".format(localpath))

    def shortcut2path(self, shortpath):
        """
        Convert short pathnames to fully qualified pathnames
        within the datastore.

        If the fully qualified localpath is
            'share/results/volumes/simulator_volume_fipnum.csv'
        then you can also access this with these alternatives:
         * simulator_volume_fipnum
         * simulator_volume_fipnum.csv
         * share/results/volumes/simulator_volume_fipnum

        but only as long as there is no ambiguity. In case
        of ambiguity, the shortpath will be returned.
        """
        basenames = list(map(os.path.basename, self.data.keys()))
        if basenames.count(shortpath) == 1:
            short2path = {os.path.basename(x): x for x in self.data}
            return short2path[shortpath]
        noexts = ["".join(x.split(".")[:-1]) for x in self.data]
        if noexts.count(shortpath) == 1:
            short2path = {"".join(x.split(".")[:-1]): x for x in self.data}
            return short2path[shortpath]
        basenamenoexts = [
            "".join(os.path.basename(x).split(".")[:-1]) for x in self.data
        ]
        if basenamenoexts.count(shortpath) == 1:
            short2path = {
                "".join(os.path.basename(x).split(".")[:-1]): x for x in self.data
            }
            return short2path[shortpath]
        # If we get here, we did not find anything that
        # this shorthand could point to. Return as is, and let the
        # calling function handle further errors.
        return shortpath

    def find_files(self, paths, metadata=None):
        """Discover realization files. The files dataframe
        will be updated.

        Certain functionality requires up-front file discovery,
        e.g. ensemble archiving and ensemble arithmetic.

        CSV files for single use does not have to be discovered.

        Args:
            paths: str or list of str with filenames (will be globbed)
                that are relative to the realization directory.
            metadata: dict with metadata to assign for the discovered
                files. The keys will be columns, and its values will be
                assigned as column values for the discovered files.
                During rediscovery of files, old metadata will be removed.
        Returns:
            A slice of the internalized dataframe corresponding
            to the discovered files (will be included even if it has
            been discovered earlier)
        """
        if isinstance(paths, str):
            paths = [paths]
        returnedslice = pd.DataFrame(
            columns=["FULLPATH", "FILETYPE", "LOCALPATH", "BASENAME"]
        )
        for searchpath in paths:
            globs = glob.glob(os.path.join(self._origpath, searchpath))
            for match in globs:
                absmatch = os.path.abspath(match)
                filerow = {
                    "LOCALPATH": os.path.relpath(match, self._origpath),
                    "FILETYPE": match.split(".")[-1],
                    "FULLPATH": absmatch,
                    "BASENAME": os.path.basename(match),
                }
                # Delete this row if it already exists, determined by FULLPATH
                if absmatch in self.files["FULLPATH"].values:
                    self.files = self.files[self.files["FULLPATH"] != absmatch]
                if metadata:
                    filerow.update(metadata)
                self.files = self.files.append(filerow, ignore_index=True)
                returnedslice = returnedslice.append(filerow, ignore_index=True)
        return returnedslice

    @property
    def parameters(self):
        """Access the data obtained from parameters.txt

        Returns:
            dict with data from parameters.txt
        """
        return self.data["parameters.txt"]

    def get_eclsum(self, cache=True, include_restart=True):
        """
        Fetch the Eclipse Summary file from the realization
        and return as a libecl EclSum object

        Unless the UNSMRY file has been discovered, it will
        pick the file from the glob `eclipse/model/*UNSMRY`,
        as long as autodiscovery is not turned off when
        the realization object was initialized.

        If you have multiple UNSMRY files in eclipse/model
        turning off autodiscovery is strongly recommended.

        Arguments:
            cache: boolean indicating whether we should keep an
                object reference to the EclSum object. Set to
                false if you need to conserve memory.
            include_restart: boolean sent to libecl for whether restarts
                files should be traversed

        Returns:
           EclSum: object representing the summary file. None if
               nothing was found.
        """
        if cache and self._eclsum:  # Return cached object if available
            if self._eclsum_include_restart == include_restart:
                return self._eclsum

        unsmry_file_row = self.files[self.files.FILETYPE == "UNSMRY"]
        unsmry_filename = None
        if len(unsmry_file_row) == 1:
            unsmry_filename = unsmry_file_row.FULLPATH.values[0]
        elif self._autodiscovery:
            unsmry_fileguess = os.path.join(self._origpath, "eclipse/model", "*.UNSMRY")
            unsmry_filenamelist = glob.glob(unsmry_fileguess)
            if not unsmry_filenamelist:
                return None  # No filename matches
            if len(unsmry_filenamelist) > 1:
                logger.warning(
                    "Multiple UNSMRY files found, "
                    + "consider turning off auto-discovery"
                )
            unsmry_filename = unsmry_filenamelist[0]
            self.find_files(unsmry_filename)
        else:
            # There is no UNSMRY file to be found.
            return None

        if not os.path.exists(unsmry_filename):
            return None
        try:
            eclsum = ecl.summary.EclSum(
                unsmry_filename, lazy_load=False, include_restart=include_restart
            )
        except IOError:
            # This can happen if there is something wrong with the file
            # or if SMSPEC is missing.
            logger.warning("Failed to create summary instance from %s", unsmry_filename)
            return None

        if cache:
            self._eclsum = eclsum
            self._eclsum_include_restart = include_restart

        return eclsum

    def load_smry(
        self,
        time_index="raw",
        column_keys=None,
        cache_eclsum=True,
        start_date=None,
        end_date=None,
        include_restart=True,
    ):
        """Produce dataframe from Summary data from the realization

        When this function is called, the dataframe will be
        internalized.  Internalization of summary data in a
        realization object supports different time_index, but there is
        no handling of multiple sets of column_keys. The cached data
        will be called

          'share/results/tables/unsmry--<time_index>.csv'

        where <time_index> is among 'yearly', 'monthly', 'daily', 'last' or
        'raw' (meaning the raw dates in the SMRY file), depending
        on the chosen time_index. If a custom time_index (list
        of datetime) was supplied, <time_index> will be called 'custom'.

        Wraps ecl.summary.EclSum.pandas_frame()

        See also get_smry()

        Args:
            time_index: string indicating a resampling frequency,
               'yearly', 'monthly', 'daily', 'last' or 'raw', the latter will
               return the simulated report steps (also default).
               If a list of DateTime is supplied, data will be resampled
               to these.
            column_keys: list of column key wildcards. None means everything.
            cache_eclsum: boolean for whether to keep the loaded EclSum
                object in memory after data has been loaded.
            start_date: str or date with first date to include.
                Dates prior to this date will be dropped, supplied
                start_date will always be included.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overriden if time_index
                is 'last'.
            include_restart: boolean sent to libecl for wheter restarts
                files should be traversed

        Returns:
            DataFrame with summary keys as columns and dates as indices.
                Empty dataframe if no summary is available or column
                keys do not exist.

        """
        if not self.get_eclsum(cache=cache_eclsum):
            # Return empty, but do not store the empty dataframe in self.data
            return pd.DataFrame()
        time_index_path = time_index
        if time_index == "raw":
            time_index_arg = None
        elif isinstance(time_index, str):
            # Note: This call will recache the smry object.
            time_index_arg = self.get_smry_dates(
                freq=time_index,
                start_date=start_date,
                end_date=end_date,
                include_restart=include_restart,
            )
        if isinstance(time_index, list):
            time_index_arg = time_index
            time_index_path = "custom"

        if not isinstance(column_keys, list):
            column_keys = [column_keys]

        # Do the actual work:
        dframe = self.get_eclsum(
            cache=cache_eclsum, include_restart=include_restart
        ).pandas_frame(time_index_arg, column_keys)
        dframe = dframe.reset_index()
        dframe.rename(columns={"index": "DATE"}, inplace=True)

        # Cache the result:
        localpath = "share/results/tables/unsmry--" + time_index_path + ".csv"
        self.data[localpath] = dframe

        # Do this to ensure that we cut the rope to the EclSum object
        # Can be critical for garbage collection
        if not cache_eclsum:
            self._eclsum = None
        return dframe

    def get_smry(
        self,
        time_index=None,
        column_keys=None,
        cache_eclsum=True,
        start_date=None,
        end_date=None,
        include_restart=True,
    ):
        """Wrapper for EclSum.pandas_frame

        This gives access to the underlying data on disk without
        touching internalized dataframes.

        Arguments:
            time_index: string indicating a resampling frequency,
               'yearly', 'monthly', 'daily', 'last' or 'raw', the latter will
               return the simulated report steps (also default).
               If a list of DateTime is supplied, data will be resampled
               to these.
            column_keys: list of column key wildcards. None means everything.
            cache_eclsum: boolean for whether to keep the loaded EclSum
                object in memory after data has been loaded.
            start_date: str or date with first date to include.
                Dates prior to this date will be dropped, supplied
                start_date will always be included.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overriden if time_index
                is 'last'.

        Returns empty dataframe if there is no summary file, or if the
        column_keys are not existing.
        """
        if not isinstance(column_keys, list):
            column_keys = [column_keys]
        if isinstance(time_index, str) and time_index == "raw":
            time_index_arg = None
        elif isinstance(time_index, str):
            time_index_arg = self.get_smry_dates(
                freq=time_index,
                start_date=start_date,
                end_date=end_date,
                include_restart=include_restart,
            )
        else:
            time_index_arg = time_index

        if self.get_eclsum(cache=cache_eclsum, include_restart=include_restart):
            try:
                dataframe = self.get_eclsum(
                    cache=cache_eclsum, include_restart=include_restart
                ).pandas_frame(time_index_arg, column_keys)
            except ValueError:
                # We get here if we have requested non-existing column keys
                return pd.DataFrame()
            if not cache_eclsum:
                # Ensure EclSum object can be garbage collected
                self._eclsum = None
            return dataframe
        else:
            return pd.DataFrame()

    def _glob_smry_keys(self, column_keys):
        """Utility function for globbing column names

        Use this to expand 'F*' to the list of Eclipse summary
        vectors matching.

        Args:
            column_keys: str or list of strings with patterns
        """
        if not isinstance(column_keys, list):
            column_keys = [column_keys]
        keys = set()
        for key in column_keys:
            if isinstance(key, str):
                keys = keys.union(set(self._eclsum.keys(key)))
        return list(keys)

    def get_volumetric_rates(self, column_keys=None, time_index=None, time_unit=None):
        """Compute volumetric rates from cumulative summary vectors

        Column names that are not referring to cumulative summary
        vectors are silently ignored.

        A Dataframe is returned with volumetric rates, that is rate
        values that can be summed up to the cumulative version. The
        'T' in the column name is switched with 'R'. If you ask for
        FOPT, you will get FOPR in the returned dataframe.

        Rates in the returned dataframe are valid **forwards** in time,
        opposed to rates coming directly from the Eclipse simulator which
        are valid backwards in time.

        If time_unit is set, the rates will be scaled to represent
        either daily, monthly or yearly rates. These will sum up to the
        cumulative as long as you multiply with the correct number
        of days, months or year between each consecutive date index.
        Month lengths and leap years are correctly handled.

        The returned dataframe is indexed by DATE.

        Args:
            column_keys: str or list of strings, cumulative summary vectors
            time_index: str or list of datetimes
            time_unit: str or None. If None, the rates returned will
                be the difference in cumulative between each included
                time step (where the time interval can vary arbitrarily)
                If set to 'days', 'months' or 'years', the rates will
                be scaled to represent a daily, monthly or yearly rate that
                is compatible with the date index and the cumulative data.

        """
        return self._get_volumetric_rates(self, column_keys, time_index, time_unit)

    @staticmethod
    def _get_volumetric_rates(realization, column_keys, time_index, time_unit):
        """Static method for volumetric rates

        This method is to be used by both ScratchRealization
        and VirtualRealization, and is documented there."""
        import calendar
        from dateutil.relativedelta import relativedelta

        if isinstance(time_unit, str):
            if time_unit not in ["days", "months", "years"]:
                raise ValueError(
                    "Unsupported time_unit " + time_unit + " for volumetric rates"
                )

        column_keys = realization._glob_smry_keys(column_keys)

        # Be strict and only include certain summary vectors that look
        # cumulative by their name:
        column_keys = [
            x for x in column_keys if ScratchRealization._cum_smrycol2rate(x)
        ]
        if not column_keys:
            logger.error(
                "No valid cumulative columns given " + "to volumetric computation"
            )
            return pd.DataFrame()

        cum_df = realization.get_smry(column_keys=column_keys, time_index=time_index)
        # get_smry() for realizations return a dataframe indexed by 'DATE'

        # Compute row-wise difference, shift back one row
        # to get the NaN to the end, and then drop the NaN.
        # The "rate" given for a specific date is then
        # valid from that date until the next date.
        diff_cum = cum_df.diff().shift(-1).fillna(value=0)

        if time_unit:
            # Calculate the relative timedelta between consecutive
            # DateIndices. relativedeltas are correct in terms
            # of number of years and number of months, but it will
            # only give us integer months, and then leftover days.
            rel_deltas = [
                relativedelta(t[1], t[0])
                for t in zip(diff_cum.index, diff_cum.index[1:])
            ]
            whole_days = [
                (t[1] - t[0]).days for t in zip(diff_cum.index, diff_cum.index[1:])
            ]
            # Need to know which years are leap years for our index:
            dayspryear = [
                365 if not calendar.isleap(x.year) else 366
                for x in pd.to_datetime(diff_cum.index[1:])
            ]
            # Float-contribution to years from days:
            days = [
                t[0] / float(t[1])
                for t in zip([r.days for r in rel_deltas], dayspryear)
            ]
            floatyearsnodays = [r.years + r.months / 12.0 for r in rel_deltas]
            floatyears = [x + y for x, y in zip(floatyearsnodays, days)]

            # Calculate month-difference:
            floatmonthsnodays = [r.years * 12.0 + r.months for r in rel_deltas]
            # How many days pr. month? We check this for the right
            # end of the relevant time interval.
            daysprmonth = [
                calendar.monthrange(t.year, t.month)[1] for t in diff_cum.index[1:]
            ]
            days = [
                t[0] / float(t[1])
                for t in zip([r.days for r in rel_deltas], daysprmonth)
            ]
            floatmonths = [x + y for x, y in zip(floatmonthsnodays, days)]

            diff_cum["DAYS"] = whole_days + [0]
            diff_cum["MONTHS"] = floatmonths + [0]
            diff_cum["YEARS"] = floatyears + [0]
            for vec in column_keys:
                diff_cum[vec] = diff_cum[vec] / diff_cum[time_unit.upper()]
            # Drop temporary columns
            diff_cum.drop(["DAYS", "MONTHS", "YEARS"], inplace=True, axis=1)
            # Set NaN at the final row to zero
            diff_cum.fillna(value=0, inplace=True)

        # Translate the column vectors, 'FOPT' -> 'FOPR' etc.
        rate_names = []
        for vec in diff_cum.columns:
            ratename = ScratchRealization._cum_smrycol2rate(vec)
            if ratename:
                rate_names.append(ratename)
        diff_cum.columns = rate_names
        diff_cum.index.name = "DATE"
        return diff_cum

    @staticmethod
    def _cum_smrycol2rate(smrycolumn):
        """Returns None if a smrycolumn is not assumed
        to be cumulative, and returns a string with the corresponding
        rate column if it is cumulative

        F.ex. _cum_smrycol2rate('FOPT') will return 'FOPR'
        """
        # Split by colon into components:
        comps = smrycolumn.split(":")
        if len(comps) > 2:
            # Do not support more than one colon.
            return None
        if "CT" in comps[0]:
            # No watercuts.
            return None
        if "T" not in comps[0]:
            return None
        comps[0] = comps[0].replace("T", "R")
        if len(comps) > 1:
            return comps[0] + ":" + comps[1]
        return comps[0]

    def get_smryvalues(self, props_wildcard=None):
        """
        Fetch selected vectors from Eclipse Summary data.

        Args:
            props_wildcard : string or list of strings with vector
                wildcards
        Returns:
            a dataframe with values. Raw times from UNSMRY.
            Empty dataframe if no summary file data available
        """
        if not self._eclsum:  # check if it is cached
            self.get_eclsum()

        if not self._eclsum:
            return pd.DataFrame()

        props = self._glob_smry_keys(props_wildcard)

        if "numpy_vector" in dir(self._eclsum):
            data = {
                prop: self._eclsum.numpy_vector(prop, report_only=False)
                for prop in props
            }
        else:  # get_values() is deprecated in newer libecl
            data = {
                prop: self._eclsum.get_values(prop, report_only=False) for prop in props
            }
        dates = self._eclsum.get_dates(report_only=False)
        return pd.DataFrame(data=data, index=dates)

    def get_smry_dates(
        self,
        freq="monthly",
        normalize=True,
        start_date=None,
        end_date=None,
        include_restart=True,
    ):
        """Return list of datetimes available in the realization

        Args:
            freq: string denoting requested frequency for
                the returned list of datetime. 'report' will
                yield the sorted union of all valid timesteps for
                all realizations. Other valid options are
                'daily', 'monthly' and 'yearly'.
                'last' will give out the last date (maximum),
                as a list with one element.
            normalize: Whether to normalize backwards at the start
                and forwards at the end to ensure the raw
                date range is covered.
            start_date: str or date with first date to include
                Dates prior to this date will be dropped, supplied
                start_date will always be included. Overrides
                normalized dates.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overrides
                normalized dates. Overriden if freq is 'last'.
        Returns:
            list of datetimes. None if no summary data is available.
        """
        from .ensemble import ScratchEnsemble

        eclsum = self.get_eclsum(include_restart=include_restart)
        if not eclsum:
            return None
        return ScratchEnsemble._get_smry_dates(
            [eclsum.dates], freq, normalize, start_date, end_date
        )

    def contains(self, localpath, **kwargs):
        """Boolean function for asking the realization for presence
        of certain data types and possibly data values.

        Args:
            localpath: string pointing to the data for which the query
                applies. If no other arguments, only realizations containing
                this data key is kept.
            key: A certain key within a realization dictionary that is
                required to be present. If a value is also provided, this
                key must be equal to this value. If localpath is not
                a dictionary, this will raise a ValueError
            value: The value a certain key must equal. Floating point
                comparisons are not robust. Only relevant for dictionaries
            column: Name of a column in tabular data. If columncontains is
                not specified, this means that this column must be present
            columncontains:
                A value that the specific column must include.

        Returns:
            boolean: True if the data is present and fulfilling any
            criteria.
        """
        kwargs.pop("inplace", 0)
        localpath = self.shortcut2path(localpath)
        if localpath not in self.keys():
            return False
        if not kwargs:
            return localpath in self.keys()
        if isinstance(self.data[localpath], dict):
            if "key" in kwargs and "value" not in kwargs:
                return kwargs["key"] in self.data[localpath]
        if isinstance(self.data[localpath], pd.DataFrame):
            if "key" in kwargs:
                raise ValueError("Don't use key for tabular data")
            if "value" in kwargs:
                raise ValueError("Don't use value for tabular data")
            if "column" in kwargs and "columncontains" not in kwargs:
                # Only asking for column presence
                return kwargs["column"] in self.data[localpath].columns
            if "column" in kwargs and "columncontains" in kwargs:
                # If we are dealing with the DATE column,
                # convert everything to pandas datatime64 for comparisons,
                # otherwise we revert to simpler check.
                if kwargs["column"] == "DATE":
                    return (
                        pd.to_datetime(dateutil.parser.parse(kwargs["columncontains"]))
                        == pd.to_datetime(self.data[localpath][kwargs["column"]])
                    ).any()
                return (
                    kwargs["columncontains"]
                    in self.data[localpath][kwargs["column"]].values
                )

        if "key" in kwargs and "value" in kwargs:
            if isinstance(kwargs["value"], str):
                if kwargs["key"] in self.data[localpath]:
                    return str(self.data[localpath][kwargs["key"]]) == kwargs["value"]
                return False
            else:  # non-string, then don't convert the internalized data
                return self.data[localpath][kwargs["key"]] == kwargs["value"]
        raise ValueError("Wrong arguments to contains()")

    def drop(self, localpath, **kwargs):
        """Delete elements from internalized data.

        Shortcuts are allowed for localpath. If the data pointed to is
        a DataFrame, you can delete columns, or rows containing certain
        elements

        If the data pointed to is a dictionary, keys can be deleted.

        Args:
            localpath: string, path to internalized data. If no other options
                are supplied, that dataset is deleted in its entirety
            column: string with a column name to drop. Only for dataframes
            columns: list of strings with column names to delete
            rowcontains: rows where one column contains this string will be
                dropped. The comparison is on strings only, and all cells in
                the dataframe is converted to strings for the comparison.
                Thus it might work on dates, but be careful with numbers.
            key: string with a keyname in a dictionary. Will not work for
                dataframes
            keys: list of strings of keys to delete from a dictionary
        """
        fullpath = self.shortcut2path(localpath)
        if fullpath not in self.keys():
            raise ValueError("%s not found" % localpath)

        data = self.data[fullpath]

        if not kwargs:
            # This will remove the entire dataset
            self.data.pop(fullpath, None)

        if isinstance(data, pd.DataFrame):
            if "column" in kwargs:
                data.drop(labels=kwargs["column"], axis="columns", inplace=True)
            if "columns" in kwargs:
                data.drop(labels=kwargs["columns"], axis="columns", inplace=True)
            if "rowcontains" in kwargs:
                # Construct boolean series for those rows that have a match
                boolseries = (data.astype(str) == str(kwargs["rowcontains"])).any(
                    axis="columns"
                )
                self.data[fullpath] = data[~boolseries]
        if isinstance(data, dict):
            if "keys" in kwargs:
                for key in kwargs["keys"]:
                    data.pop(key, None)
            if "key" in kwargs:
                data.pop(kwargs["key"], None)

    def __repr__(self):
        """Represent the realization. Show only the last part of the path"""
        pathsummary = self._origpath[-50:]
        if self.index:
            indexstr = str(self.index)
        else:
            indexstr = "Error"
        return "<Realization, index={}, path=...{}>".format(indexstr, pathsummary)

    def __sub__(self, other):
        result = RealizationCombination(ref=self, sub=other)
        return result

    def __add__(self, other):
        result = RealizationCombination(ref=self, add=other)
        return result

    def __mul__(self, other):
        result = RealizationCombination(ref=self, scale=float(other))
        return result

    def __rsub__(self, other):
        result = RealizationCombination(ref=self, sub=other)
        return result

    def __radd__(self, other):
        result = RealizationCombination(ref=self, add=other)
        return result

    def __rmul__(self, other):
        result = RealizationCombination(ref=self, scale=float(other))
        return result

    def get_init(self):
        """
        :returns: init file of the realization.
        """
        init_file_row = self.files[self.files.FILETYPE == "INIT"]
        init_filename = None
        if len(init_file_row) == 1:
            init_filename = init_file_row.FULLPATH.values[0]
        else:
            init_fileguess = os.path.join(self._origpath, "eclipse/model", "*.INIT")
            init_filenamelist = glob.glob(init_fileguess)
            if not init_filenamelist:
                return None  # No filename matches
            init_filename = init_filenamelist[0]
        if not os.path.exists(init_filename):
            return None

        if not self._eclinit:
            self._eclinit = EclFile(
                init_filename, flags=EclFileFlagEnum.ECL_FILE_CLOSE_STREAM
            )
        return self._eclinit

    def get_unrst(self):
        """
        :returns: restart file of the realization.
        """
        unrst_file_row = self.files[self.files.FILETYPE == "UNRST"]
        unrst_filename = None
        if len(unrst_file_row) == 1:
            unrst_filename = unrst_file_row.FULLPATH.values[0]
        else:
            unrst_fileguess = os.path.join(self._origpath, "eclipse/model", "*.UNRST")
            unrst_filenamelist = glob.glob(unrst_fileguess)
            if not unrst_filenamelist:
                return None  # No filename matches
            unrst_filename = unrst_filenamelist[0]
        if not os.path.exists(unrst_filename):
            return None
        if not self._eclunrst:
            self._eclunrst = EclFile(
                unrst_filename, flags=EclFileFlagEnum.ECL_FILE_CLOSE_STREAM
            )
        return self._eclunrst

    def get_grid_index(self, active_only):
        """
        Return the grid index in a pandas dataframe.
        """
        return self.get_grid().export_index(active_only=active_only)

    def get_grid_corners(self, grid_index):
        """Return a dataframe with the the x, y, z for the
        8 grid corners of corner point cells"""
        corners = self.get_grid().export_corners(grid_index)
        columns = [
            "x1",
            "y1",
            "z1",
            "x2",
            "y2",
            "z2",
            "x3",
            "y3",
            "z3",
            "x4",
            "y4",
            "z4",
            "x5",
            "y5",
            "z5",
            "x6",
            "y6",
            "z6",
            "x7",
            "y7",
            "z7",
            "x8",
            "y8",
            "z8",
        ]

        return pd.DataFrame(data=corners, columns=columns)

    def get_grid_centre(self, grid_index):
        """Return the grid centre of corner-point-cells, x, y and z
        in distinct columns"""
        grid_cell_centre = self.get_grid().export_position(grid_index)
        return pd.DataFrame(
            data=grid_cell_centre, columns=["cell_x", "cell_y", "cell_z"]
        )

    def get_grid(self):
        """
        :returns: grid file of the realization.
        """
        grid_file_row = self.files[self.files.FILETYPE == "EGRID"]
        grid_filename = None
        if len(grid_file_row) == 1:
            grid_filename = grid_file_row.FULLPATH.values[0]
        else:
            grid_fileguess = os.path.join(self._origpath, "eclipse/model", "*.EGRID")
            grid_filenamelist = glob.glob(grid_fileguess)
            if not grid_filenamelist:
                return None  # No filename matches
            grid_filename = grid_filenamelist[0]
        if not os.path.exists(grid_filename):
            return None
        if not self._eclgrid:
            self._eclgrid = EclGrid(grid_filename)
        return self._eclgrid

    @property
    def global_size(self):
        """
        :returns: Number of cells in the realization.
        """
        return self.get_grid().get_global_size()

    @property
    def actnum(self):
        """
        :returns: EclKw of ints showing which cells are active,
            Active cells are given value 1, while
            inactive cells have value 1.
        """
        if not self._actnum:
            self._actnum = self.get_init()["PORV"][0].create_actnum()
        return self._actnum

    @property
    def report_dates(self):
        """
        :returns: List of DateTime.DateTime for which values are reported.
        """
        return self.get_unrst().report_dates

    def get_global_init_keyword(self, prop):
        """
        :param prop: A name of a keyword in the realization's init file.
        :returns: The EclKw of given name. Length is global_size.
            non-active cells are given value 0.
        """
        return self.get_init()[prop][0].scatter_copy(self.actnum)

    def get_global_unrst_keyword(self, prop, report):
        """
        :param prop: A name of a keyword in the realization's restart file.
        :returns: The EclKw of given name. Length is global_size.
            non-active cells are given value 0.
        """
        return self.get_unrst()[prop][report].scatter_copy(self.actnum)


def normalize_dates(start_date, end_date, freq):
    """
    Normalize start and end date according to frequency
    by extending the time range.

    So for [1997-11-5, 2020-03-02] and monthly freqency
    this will transform your dates to
    [1997-11-1, 2020-04-01]

    For yearly frequency will be [1997-01-01, 2021-01-01].

    Args:
        start_date: datetime.date
        end_date: datetime.date
        freq: string with either 'monthly' or 'yearly'.
            Anything else will return the input as is
    Return:
        Tuple of normalized (start_date, end_date)
    """
    from dateutil.relativedelta import relativedelta

    if freq == "monthly":
        start_date = start_date.replace(day=1)

        # Avoid rolling forward if we are already at day 1 in a month
        if end_date != end_date.replace(day=1):
            end_date = end_date.replace(day=1) + relativedelta(months=1)
    elif freq == "yearly":
        start_date = start_date.replace(day=1, month=1)
        # Avoid rolling forward if we are already at day 1 in a year
        if end_date != end_date.replace(day=1, month=1):
            end_date = end_date.replace(day=1, month=1) + relativedelta(years=1)
    elif freq == "daily":
        # This we don't need to normalize, but we should not give any warnings
        pass
    elif freq == "last":
        # This we don't need to normalize, but we should not give any warnings
        pass
    else:
        logger.warning("Unrecognized frequency %s for date normalization", str(freq))
    return (start_date, end_date)


def parse_number(value):
    """Try to parse the string first as an integer, then as float,
    if both fails, return the original string.

    Caveats: Know your Python numbers:
    https://stackoverflow.com/questions/379906/how-do-i-parse-a-string-to-a-float-or-int-in-python

    Beware, this is a minefield.

    Returns:
        int, float or string
    """
    if isinstance(value, int):
        return value
    elif isinstance(value, float):
        # int(afloat) fails on some, e.g. NaN
        try:
            if int(value) == value:
                return int(value)
            return value
        except ValueError:
            return value  # return float
    else:  # noqa
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value
