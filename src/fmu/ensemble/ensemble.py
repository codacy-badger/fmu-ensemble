# -*- coding: utf-8 -*-
"""Module containing the ScratchEnsemble class
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import re
import os
import glob
import six

from datetime import datetime, date, time
import pandas as pd
import numpy as np
from ecl import EclDataType
from ecl.eclfile import EclKW

from .etc import Interaction
from .realization import ScratchRealization
from .virtualrealization import VirtualRealization
from .virtualensemble import VirtualEnsemble
from .ensemblecombination import EnsembleCombination
from .realization import parse_number

xfmu = Interaction()
logger = xfmu.functionlogger(__name__)


class ScratchEnsemble(object):
    """An ensemble is a collection of Realizations.

    Ensembles are initialized from path(s) pointing to
    filesystem locations containing realizations.

    Ensemble objects can be grouped into EnsembleSet.

    Realizations in an ensembles are uniquely determined
    by their realization index (integer).

    Example for initialization:
        >>> from fmu import ensemble
        >>> ens = ensemble.ScratchEnsemble('ensemblename',
                    '/scratch/fmu/foobert/r089/casename/realization-*/iter-0')

    Upon initialization, only a subset of the files on
    disk will be discovered. More files must be expliclitly
    discovered and/or loaded.

    Args:
        ensemble_name (str): Name identifier for the ensemble.
            Optional to have it consistent with f.ex. iter-0 in the path.
        paths (list/str): String or list of strings with wildcards
            to file system. Absolute or relative paths.
            If omitted, ensemble will be empty unless runpathfile
            is used.
        realidxregexp: str or regexp - used to deduce the realization index
            from the file path. Default tailored for realization-X
        runpathfile: str. Filename (absolute or relative) of an ERT
            runpath file, consisting of four space separated text fields,
            first column is realization index, second column is absolute
            or relative path to a realization RUNPATH, third column is
            the basename of the Eclipse simulation, relative to RUNPATH.
            Fourth column is not used.
        runpathfilter: str. If supplied, the only the runpaths in
            the runpathfile which contains this string will be included
            Use to select only a specific realization f.ex.
        autodiscovery: boolean. True by default, means that the class
            can try to autodiscover data in the realization. Turn
            off to gain more fined tuned control.
    """

    def __init__(
        self,
        ensemble_name,
        paths=None,
        realidxregexp=None,
        runpathfile=None,
        runpathfilter=None,
        autodiscovery=True,
    ):
        self._name = ensemble_name  # ensemble name
        self._realizations = {}  # dict of ScratchRealization objects,
        # indexed by realization indices as integers.
        self._ens_df = pd.DataFrame()
        self._global_active = None
        self._global_size = None
        self._global_grid = None
        self.obs = None

        if isinstance(paths, str):
            paths = [paths]

        if paths and runpathfile:
            logger.error("Cannot initialize from both path and runpathfile")
            return

        globbedpaths = None
        if isinstance(paths, list):
            # Glob incoming paths to determine
            # paths for each realization (flatten and uniqify)
            globbedpaths = [glob.glob(path) for path in paths]
            globbedpaths = list(
                set([item for sublist in globbedpaths for item in sublist])
            )
        if not globbedpaths:
            if isinstance(runpathfile, str):
                if not runpathfile:
                    logger.warning("Initialized empty ScratchEnsemble")
                    return
            if isinstance(runpathfile, pd.DataFrame):
                if runpathfile.empty:
                    logger.warning("Initialized empty ScratchEnsemble")
                    return

        count = None
        if globbedpaths:
            logger.info("Loading ensemble from dirs: %s", " ".join(globbedpaths))

            # Search and locate minimal set of files
            # representing the realizations.
            count = self.add_realizations(
                paths, realidxregexp, autodiscovery=autodiscovery
            )

        if isinstance(runpathfile, str) and runpathfile:
            count = self.add_from_runpathfile(runpathfile, runpathfilter)
        if isinstance(runpathfile, pd.DataFrame) and not runpathfile.empty:
            count = self.add_from_runpathfile(runpathfile, runpathfilter)

        if count:
            logger.info("ScratchEnsemble initialized with %d realizations", count)
        else:
            logger.warning("ScratchEnsemble empty")

    def __getitem__(self, realizationindex):
        """Get one of the ScratchRealization objects.

        Indexed by integers."""
        return self._realizations[realizationindex]

    def keys(self):
        """
        Return the union of all keys available in realizations.

        Keys refer to the realization datastore, a dictionary
        of dataframes or dicts. Examples would be `parameters.txt`,
        `STATUS`, `share/results/tables/unsmry--monthly.csv`
        """
        allkeys = set()
        for realization in self._realizations.values():
            allkeys = allkeys.union(realization.keys())
        return list(allkeys)

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
        return self._shortcut2path(self.keys(), shortpath)

    @staticmethod
    def _shortcut2path(keys, shortpath):
        basenames = list(map(os.path.basename, keys))
        if basenames.count(shortpath) == 1:
            short2path = {os.path.basename(x): x for x in keys}
            return short2path[shortpath]
        noexts = ["".join(x.split(".")[:-1]) for x in keys]
        if noexts.count(shortpath) == 1:
            short2path = {"".join(x.split(".")[:-1]): x for x in keys}
            return short2path[shortpath]
        basenamenoexts = ["".join(os.path.basename(x).split(".")[:-1]) for x in keys]
        if basenamenoexts.count(shortpath) == 1:
            short2path = {"".join(os.path.basename(x).split(".")[:-1]): x for x in keys}
            return short2path[shortpath]
        # If we get here, we did not find anything that
        # this shorthand could point to. Return as is, and let the
        # calling function handle further errors.
        return shortpath

    def add_realizations(self, paths, realidxregexp=None, autodiscovery=True):
        """Utility function to add realizations to the ensemble.

        Realizations are identified by their integer index.
        If the realization index already exists, it will be replaced
        when calling this function.

        This function passes on initialization to ScratchRealization
        and stores a reference to those generated objects.

        Args:
            paths (list/str): String or list of strings with wildcards
                to file system. Absolute or relative paths.
            autodiscovery: boolean, whether files can be attempted
                auto-discovered

        Returns:
            count (int): Number of realizations successfully added.
        """
        if isinstance(paths, list):
            globbedpaths = [glob.glob(path) for path in paths]
            # Flatten list and uniquify:
            globbedpaths = list(
                set([item for sublist in globbedpaths for item in sublist])
            )
        else:
            globbedpaths = glob.glob(paths)

        count = 0
        for realdir in globbedpaths:
            realization = ScratchRealization(
                realdir, realidxregexp=realidxregexp, autodiscovery=autodiscovery
            )
            if realization.index is None:
                logger.critical(
                    "Could not determine realization index " + "for path " + realdir
                )
                if not realidxregexp:
                    logger.critical("Maybe you need to supply a regexp.")
                else:
                    logger.critical("Your regular expression is maybe wrong.")
            else:
                count += 1
                self._realizations[realization.index] = realization
        logger.info("add_realizations() found %d realizations", len(self._realizations))
        return count

    def add_from_runpathfile(self, runpath, runpathfilter=None):
        """Add realizations from a runpath file typically
        coming from ERT.

        The runpath file is a space separated table with the columns:

          * index - integer with realization index
          * runpath - string with the full path to the realization
          * eclbase - ECLBASE within the runpath (location of DATA file
            minus the trailing '.DATA')
          * iter - integer with the iteration number.

        Args:
            runpath: str with filename, absolute or relative, or
                a Pandas DataFrame parsed from a runpath file
            runpathfilter: str which each filepath has to match
                in order to be included. Default None which means not filter

        Returns:
            int - Number of successfully added realizations.
        """
        prelength = len(self)
        if isinstance(runpath, str):
            runpath_df = pd.read_csv(
                runpath,
                sep=r"\s+",
                engine="python",
                names=["index", "runpath", "eclbase", "iter"],
            )
        elif isinstance(runpath, pd.DataFrame):
            # We got a readymade dataframe. Perhaps a slice.
            # Most likely we are getting the slice from an EnsembleSet
            # initialization.
            runpath_df = runpath
            if (
                "index" not in runpath_df
                or "runpath" not in runpath_df
                or "eclbase" not in runpath_df
                or "iter" not in runpath_df
            ):
                raise ValueError("runpath dataframe not correct")

        for idx, row in runpath_df.iterrows():
            if runpathfilter and runpathfilter not in row["runpath"]:
                continue
            logger.info("Adding realization from " + row["runpath"])
            realization = ScratchRealization(
                row["runpath"], index=int(row["index"]), autodiscovery=False
            )
            # Use the ECLBASE from the runpath file to
            # ensure we recognize the correct UNSMRY file
            realization.find_files(row["eclbase"] + ".DATA")
            realization.find_files(row["eclbase"] + ".UNSMRY")
            self._realizations[int(row["index"])] = realization

        return len(self) - prelength

    def remove_data(self, localpaths):
        """Remove certain datatypes from each realizations
        datastores. This modifies the underlying realization
        objects, and is equivalent to

        >>> del realization[localpath]

        on each realization in the ensemble.

        Args:
            localpath: string with full localpath to
                the data, or list of strings.
        """
        if isinstance(localpaths, str):
            localpaths = [localpaths]
        for localpath in localpaths:
            for _, real in self._realizations.items():
                del real[localpath]

    def remove_realizations(self, realindices):
        """Remove specific realizations from the ensemble

        Args:
            realindices: int or list of ints for the realization
                indices to be removed
        """
        if isinstance(realindices, int):
            realindices = [realindices]
        popped = 0
        for index in realindices:
            self._realizations.pop(index, None)
            popped += 1
        logger.info("removed %d realization(s)", popped)

    def to_virtual(self, name=None):
        """Convert the ScratchEnsemble to a VirtualEnsemble.

        This means that all imported data in each realization is
        aggregated and stored as dataframes in the returned
        VirtualEnsemble
        """
        vens = VirtualEnsemble(name=name)

        for key in self.keys():
            vens.append(key, self.get_df(key))
        vens.update_realindices()
        return vens

    @property
    def parameters(self):
        """Getter for get_parameters(convert_numeric=True)
        """
        return self.load_txt("parameters.txt")

    def load_scalar(self, localpath, convert_numeric=False, force_reread=False):
        """Parse a single value from a file for each realization.

        The value can be a string or a number.

        Empty files are treated as existing, with an empty string as
        the value, different from non-existing files.

        Parsing is performed individually in each realization

        Args:
            localpath: path to the text file, relative to each realization
            convert_numeric: If set to True, assume that
                the value is numerical, and treat strings as
                errors.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.
        Returns:
            DataFrame, with aggregated data over the ensemble. The column 'REAL'
                signifies the realization indices, and a column with the same
                name as the localpath filename contains the data.

        """
        return self.load_file(localpath, "scalar", convert_numeric, force_reread)

    def load_txt(self, localpath, convert_numeric=True, force_reread=False):
        """Parse a key-value text file from disk and internalize data

        Parses text files on the form
        <key> <value>
        in each line.

        Parsing is performed individually in each realization
        """
        return self.load_file(localpath, "txt", convert_numeric, force_reread)

    def load_csv(self, localpath, convert_numeric=True, force_reread=False):
        """For each realization, load a CSV.

        The CSV file must be present in at least one realization.
        The parsing is done individually for each realization, and
        aggregation is on demand (through get_df()) and when
        this function returns.

        Args:
            localpath: path to the text file, relative to each realization
            convert_numeric: If set to True, numerical columns
                will be searched for and have their dtype set
                to integers or floats. If scalars, only numerical
                data will be loaded.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.
        Returns:
            Dataframe, aggregation of the loaded CSV files. Column 'REAL'
                distuinguishes each realizations data.
        """
        return self.load_file(localpath, "csv", convert_numeric, force_reread)

    def load_file(self, localpath, fformat, convert_numeric=False, force_reread=False):
        """Function for calling load_file() in every realization

        This function may utilize multithreading.

        Args:
            localpath: path to the text file, relative to each realization
            fformat: string identifying the file format. Supports 'txt'
                and 'csv'.
            convert_numeric: If set to True, numerical columns
                will be searched for and have their dtype set
                to integers or floats. If scalars, only numerical
                data will be loaded.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.
        Returns:
            Dataframe with loaded data aggregated. Column 'REAL'
                distuinguishes each realizations data.
        """
        for index, realization in self._realizations.items():
            try:
                realization.load_file(localpath, fformat, convert_numeric, force_reread)
            except ValueError:
                # This would at least occur for unsupported fileformat,
                # and that we should not skip.
                logger.critical("load_file() failed in realization %d", index)
                raise ValueError
            except IOError:
                # At ensemble level, we allow files to be missing in
                # some realizations
                logger.warning("Could not read %s for realization %d", localpath, index)
        if self.get_df(localpath).empty:
            raise ValueError("No ensemble data found for %s", localpath)
        return self.get_df(localpath)

    def find_files(self, paths, metadata=None):
        """Discover realization files. The files dataframes
        for each realization will be updated.

        Certain functionality requires up-front file discovery,
        e.g. ensemble archiving and ensemble arithmetic.

        CSV files for single use does not have to be discovered.

        Args:
            paths: str or list of str with filenames (will be globbed)
                that are relative to the realization directory.
            metadata: dict with metadata to assign for the discovered
                files. The keys will be columns, and its values will be
                assigned as column values for the discovered files.
        Returns:
            DataFrame with the slice of discovered files in each
            realization, tagged with realization index in the column REAL
        """
        df_list = {}
        for index, realization in self._realizations.items():
            df_list[index] = realization.find_files(paths, metadata)
        if df_list:
            return (
                pd.concat(df_list, sort=False)
                .reset_index()
                .rename(columns={"level_0": "REAL"})
                .drop("level_1", axis="columns")
            )

    def __repr__(self):
        return "<ScratchEnsemble {}, {} realizations>".format(self.name, len(self))

    def __len__(self):
        return len(self._realizations)

    def get_smrykeys(self, vector_match=None):
        """
        Return a union of all Eclipse Summary vector names
        in all realizations (union).

        Args:
            vector_match: `Optional`. String (or list of strings)
               with wildcard filter. If None, all vectors are returned
        Returns:
            list of strings with summary vectors. Empty list if no
            summary file or no matched summary file vectors
        """
        if isinstance(vector_match, str):
            vector_match = [vector_match]
        result = set()
        for index, realization in self._realizations.items():
            eclsum = realization.get_eclsum()
            if eclsum:
                if vector_match is None:
                    result = result.union(set(eclsum.keys()))
                else:
                    for vector in vector_match:
                        result = result.union(set(eclsum.keys(vector)))
            else:
                logger.warning("No EclSum available for realization %d", index)
        return list(result)

    def get_df(self, localpath):
        """Load data from each realization and aggregate (vertically)

        Data must be already have been internalized using
        a load_*() function.

        Each row is tagged by the realization index in the column 'REAL'

        Args:
            localpath: string, refers to the internalized name.
        Returns:
           dataframe: Merged data from each realization.
               Realizations with missing data are ignored.
               Empty dataframe if no data is found
        """
        dflist = {}
        for index, realization in self._realizations.items():
            try:
                data = realization.get_df(localpath)
                if isinstance(data, dict):
                    data = pd.DataFrame(index=[1], data=data)
                elif isinstance(data, (str, int, float, np.integer, np.floating)):
                    data = pd.DataFrame(index=[1], columns=[localpath], data=data)
                if isinstance(data, pd.DataFrame):
                    dflist[index] = data
                else:
                    raise ValueError("Unkown datatype returned " + "from realization")
            except ValueError:
                # No logging here, those error messages
                # should have appeared at construction using load_*()
                pass
        if dflist:
            # Merge a dictionary of dataframes. The dict key is
            # the realization index, and end up in a MultiIndex
            dframe = pd.concat(dflist, sort=False).reset_index()
            dframe.rename(columns={"level_0": "REAL"}, inplace=True)
            del dframe["level_1"]  # This is the indices from each real
            return dframe
        else:
            raise ValueError("No data found for " + localpath)

    def load_smry(
        self,
        time_index="raw",
        column_keys=None,
        stacked=True,
        cache_eclsum=True,
        start_date=None,
        end_date=None,
        include_restart=True,
    ):
        """
        Fetch and internalize summary data from all realizations.

        The fetched summary data will be cached/internalized by each
        realization object, and can be retrieved through get_df().

        The name of the internalized dataframe is "unsmry--" + a string
        for the time index, 'monthly', 'yearly', 'daily' or 'raw'.

        Multiple calls to this function with differnent time indices
        will lead to multiple storage of internalized dataframes, so
        your ensemble can both contain a yearly and a monthly dataset.
        There is no requirement for the column_keys to be consistent, but
        care should be taken if they differ.

        If you create a virtual ensemble of this ensemble object, all
        internalized summary data will be kept, as opposed to if
        you have retrieved it through get_smry()

        Wraps around Realization.load_smry() which wraps around
        ecl.summary.EclSum.pandas_frame()

        Beware that the default time_index for ensembles is 'monthly',
        differing from realizations which use raw dates by default.

        Args:
            time_index: list of DateTime if interpolation is wanted.
                If defaulted, the raw Eclipse report times will be used.
                If a string is supplied, that string is attempted used
                via get_smry_dates() in order to obtain a time index,
                typically 'monthly', 'daily' or 'yearly'.
            column_keys: str or list of column key wildcards. Default is '*'
                which will match all vectors in the Eclipse output.
            stacked: boolean determining the dataframe layout. If
                true, the realization index is a column, and dates are repeated
                for each realization in the DATES column.
                If false, a dictionary of dataframes is returned, indexed
                by vector name, and with realization index as columns.
                This only works when time_index is the same for all
                realizations. Not implemented yet!
            cache_eclsum: Boolean for whether we should cache the EclSum
                objects. Set to False if you cannot keep all EclSum files in
                memory simultaneously
            start_date: str or date with first date to include.
                Dates prior to this date will be dropped, supplied
                start_date will always be included. If string, use
                ISO-format, YYYY-MM-DD.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overriden if time_index
                is 'last'. If string, use ISO-format, YYYY-MM-DD.
            include_restart: boolean sent to libecl for wheter restarts
                files should be traversed
        Returns:
            A DataFame of summary vectors for the ensemble, or
            a dict of dataframes if stacked=False.
        """
        if not stacked:
            raise NotImplementedError
        # Future: Multithread this!
        for realidx, realization in self._realizations.items():
            # We do not store the returned DataFrames here,
            # instead we look them up afterwards using get_df()
            # Downside is that we have to compute the name of the
            # cached object as it is not returned.
            logger.info("Loading smry from realization %s", realidx)
            realization.load_smry(
                time_index=time_index,
                column_keys=column_keys,
                cache_eclsum=cache_eclsum,
                start_date=start_date,
                end_date=end_date,
                include_restart=include_restart,
            )
        if isinstance(time_index, list):
            time_index = "custom"
        return self.get_df("share/results/tables/unsmry--" + time_index + ".csv")

    def get_volumetric_rates(self, column_keys=None, time_index=None):
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

        Args:
            column_keys: str or list of strings, cumulative summary vectors
            time_index: str or list of datetimes

        Returns:
            DataFrame analoguous to the dataframe returned by get_smry().
            Empty dataframe if no data found.
        """
        vol_dfs = []
        for realidx, real in self._realizations.items():
            vol_real = real.get_volumetric_rates(
                column_keys=column_keys, time_index=time_index
            )
            if "DATE" not in vol_real.columns and vol_real.index.name == "DATE":
                # This should be true, if not we might be in trouble.
                vol_real.reset_index(inplace=True)
            vol_real.insert(0, "REAL", realidx)
            vol_dfs.append(vol_real)

        if not vol_dfs:
            return pd.DataFrame()
        return pd.concat(vol_dfs, ignore_index=True, sort=False)

    def filter(self, localpath, inplace=True, **kwargs):
        """Filter realizations or data within realizations

        Calling this function can return a copy with fewer
        realizations, or remove realizations from the current object.

        Typical usage is to require that parameters.txt is present, or
        that the OK file is present.

        It is also possible to require a certain scalar to have a specific
        value, for example filtering on a specific sensitivity case.

        Args:
            localpath: string pointing to the data for which the filtering
                applies. If no other arguments, only realizations containing
                this data key is kept.
            key: A certain key within a realization dictionary that is
                required to be present. If a value is also provided, this
                key must be equal to this value
            value: The value a certain key must equal. Floating point
                comparisons are not robust.
            column: Name of a column in tabular data. If columncontains is
                not specified, this means that this column must be present
            columncontains:
                A value that the specific column must include.
            inplace: Boolean indicating if the current object should have its
                realizations stripped, or if a copy should be returned.
                Default true.

         Return:
            If inplace=True, then nothing will be returned.
            If inplace=False, a VirtualEnsemble fulfilling the filter
            will be returned.
        """
        deletethese = []
        keepthese = []
        for realidx, realization in self._realizations.items():
            if inplace:
                if not realization.contains(localpath, **kwargs):
                    deletethese.append(realidx)
            else:
                if realization.contains(localpath, **kwargs):
                    keepthese.append(realidx)

        if inplace:
            logger.info("Removing realizations %s", deletethese)
            if deletethese:
                self.remove_realizations(deletethese)
            return self
        else:
            filtered = VirtualEnsemble(self.name + " filtered")
            for realidx in keepthese:
                filtered.add_realization(self._realizations[realidx])
            return filtered

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
        if self.shortcut2path(localpath) not in self.keys():
            raise ValueError("%s not found" % localpath)
        for _, realization in self._realizations.items():
            try:
                realization.drop(localpath, **kwargs)
            except ValueError:
                pass  # Allow localpath to be missing in some realizations

    def apply(self, callback, **kwargs):
        """Callback functionalty, apply a function to every realization

        The supplied function handle will be handed over to
        each underlying realization object. The function supplied
        must return a Pandas DataFrame. The function can obtain
        the realization object in the kwargs dictionary through
        the key 'realization'.

        Args:
            callback: function handle
            kwargs: dictionary where 'realization' and
                'localpath' is reserved, will be forwarded
                to the callbacked function
            localpath: str, optional if the data is to be internalized
                in each realization object.

        Returns:
            pd.DataFrame, aggregated result of the supplied function
                on each realization.
        """
        results = []
        for realidx, realization in self._realizations.items():
            result = realization.apply(callback, **kwargs).copy()
            # (we took a copy since we are modifying it here:)
            result["REAL"] = realidx
            results.append(result)
        return pd.concat(results, sort=False, ignore_index=True)

    def get_smry_dates(
        self,
        freq="monthly",
        normalize=True,
        start_date=None,
        end_date=None,
        cache_eclsum=True,
        include_restart=True,
    ):
        """Return list of datetimes for an ensemble according to frequency

        Args:
           freq: string denoting requested frequency for
               the returned list of datetime. 'report' or 'raw' will
               yield the sorted union of all valid timesteps for
               all realizations. Other valid options are
               'daily', 'monthly' and 'yearly'.
               'last' will give out the last date (maximum).
            normalize:  Whether to normalize backwards at the start
                and forwards at the end to ensure the raw
                date range is covered.
            start_date: str or date with first date to include.
                Dates prior to this date will be dropped, supplied
                start_date will always be included. Overrides
                normalized dates. If string, use ISO-format, YYYY-MM-DD.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overrides
                normalized dates. Overriden if freq is 'last'.
                If string, use ISO-format, YYYY-MM-DD.
            include_restart: boolean sent to libecl for wheter restarts
                files should be traversed

        Returns:
            list of datetimes. Empty list if no data found.
        """

        # Build list of list of eclsum dates
        eclsumsdates = []
        for _, realization in self._realizations.items():
            if realization.get_eclsum(
                cache=cache_eclsum, include_restart=include_restart
            ):
                eclsumsdates.append(
                    realization.get_eclsum(
                        cache=cache_eclsum, include_restart=include_restart
                    ).dates
                )
        return ScratchEnsemble._get_smry_dates(
            eclsumsdates, freq, normalize, start_date, end_date
        )

    @staticmethod
    def _get_smry_dates(eclsumsdates, freq, normalize, start_date, end_date):
        """Internal static method to be used by ScratchEnsemble and
        ScratchRealization.

        If called from ScratchRealization, the list of eclsums passed
        in will have length 1, if not, it can be larger.

        """
        import dateutil.parser
        from .realization import normalize_dates

        if not eclsumsdates:
            return []

        if start_date:
            if isinstance(start_date, str):
                start_date = dateutil.parser.parse(start_date).date()
            elif isinstance(start_date, datetime.date):
                pass
            else:
                raise TypeError("start_date had unknown type")

        if end_date:
            if isinstance(end_date, str):
                end_date = dateutil.parser.parse(end_date).date()
            elif isinstance(end_date, datetime.date):
                pass
            else:
                raise TypeError("end_date had unknown type")

        if freq == "report" or freq == "raw":
            datetimes = set()
            for eclsumdatelist in eclsumsdates:
                datetimes = datetimes.union(eclsumdatelist)
            datetimes = list(datetimes)
            datetimes.sort()
            if start_date:
                # Convert to datetime (at 00:00:00)
                start_date = datetime.combine(start_date, datetime.min.time())
                datetimes = [x for x in datetimes if x > start_date]
                datetimes = [start_date] + datetimes
            if end_date:
                end_date = datetime.combine(end_date, datetime.min.time())
                datetimes = [x for x in datetimes if x < end_date]
                datetimes = datetimes + [end_date]
            return datetimes
        elif freq == "last":
            end_date = max([max(x) for x in eclsumsdates]).date()
            return [end_date]
        else:
            # These are datetime.datetime, not datetime.date
            start_smry = min([min(x) for x in eclsumsdates])
            end_smry = max([max(x) for x in eclsumsdates])

            pd_freq_mnenomics = {"monthly": "MS", "yearly": "YS", "daily": "D"}

            (start_n, end_n) = normalize_dates(start_smry.date(), end_smry.date(), freq)

            if not start_date and not normalize:
                start_date_range = start_smry.date()
            elif not start_date and normalize:
                start_date_range = start_n
            else:
                start_date_range = start_date

            if not end_date and not normalize:
                end_date_range = end_smry.date()
            elif not end_date and normalize:
                end_date_range = end_n
            else:
                end_date_range = end_date

            if freq not in pd_freq_mnenomics:
                raise ValueError("Requested frequency %s not supported" % freq)
            datetimes = pd.date_range(
                start_date_range, end_date_range, freq=pd_freq_mnenomics[freq]
            )
            # Convert from Pandas' datetime64 to datetime.date:
            datetimes = [x.date() for x in datetimes]

            # pd.date_range will not include random dates that do not
            # fit on frequency boundary. Force include these if
            # supplied as user arguments.
            if start_date and start_date not in datetimes:
                datetimes = [start_date] + datetimes
            if end_date and end_date not in datetimes:
                datetimes = datetimes + [end_date]
            return datetimes

    def get_smry_stats(
        self,
        column_keys=None,
        time_index="monthly",
        quantiles=None,
        cache_eclsum=True,
        start_date=None,
        end_date=None,
    ):
        """
        Function to extract the ensemble statistics (Mean, Min, Max, P10, P90)
        for a set of simulation summary vectors (column key).

        Compared to the agg() function, this function only works on summary
        data (time series), and will only operate on actually requested data,
        independent of what is internalized. It accesses the summary files
        directly and can thus obtain data at any time frequency.

        Args:
            column_keys: list of column key wildcards
            time_index: list of DateTime if interpolation is wanted
               default is None, which returns the raw Eclipse report times
               If a string is supplied, that string is attempted used
               via get_smry_dates() in order to obtain a time index.
            quantiles: list of ints between 0 and 100 for which quantiles
               to compute. Quantiles refer to scientific standard, which
               is opposite to the oil industry convention.
               Ask for p10 if you need the oil industry p90.
            cache_eclsum: boolean for whether to keep the loaded EclSum
                object in memory after data has been loaded.
            start_date: str or date with first date to include.
                Dates prior to this date will be dropped, supplied
                start_date will always be included. If string,
                use ISO-format, YYYY-MM-DD.
            end_date: str or date with last date to be included.
                Dates past this date will be dropped, supplied
                end_date will always be included. Overriden if time_index
                is 'last'. If string, use ISO-format, YYYY-MM-DD.
        Returns:
            A MultiIndex dataframe. Outer index is 'minimum', 'maximum',
            'mean', 'p10', 'p90', inner index are the dates. Column names
            are the different vectors. Quantiles refer to the scientific
            standard, opposite to the oil industry convention.
            If quantiles are explicitly supplied, the 'pXX'
            strings in the outer index are changed accordingly. If no
            data is found, return empty DataFrame.

        TODO: add warning message when failed realizations are removed
        """

        if quantiles is None:
            quantiles = [10, 90]

        # Check validity of quantiles to compute:
        quantiles = list(map(int, quantiles))  # Potentially raise ValueError
        for quantile in quantiles:
            if quantile < 0 or quantile > 100:
                raise ValueError("Quantiles must be integers " + "between 0 and 100")

        # Obtain an aggregated dataframe for only the needed columns over
        # the entire ensemble.
        dframe = self.get_smry(
            time_index=time_index,
            column_keys=column_keys,
            cache_eclsum=cache_eclsum,
            start_date=start_date,
            end_date=end_date,
        )
        if "REAL" in dframe:
            dframe = dframe.drop(columns="REAL").groupby("DATE")
        else:
            logger.warning("No data found for get_smry_stats")
            return pd.DataFrame()

        # Build a dictionary of dataframes to be concatenated
        dframes = {}
        dframes["mean"] = dframe.mean()
        for quantile in quantiles:
            quantile_str = "p" + str(quantile)
            dframes[quantile_str] = dframe.quantile(q=quantile / 100.0)
        dframes["maximum"] = dframe.max()
        dframes["minimum"] = dframe.min()

        return pd.concat(dframes, names=["STATISTIC"], sort=False)

    def get_wellnames(self, well_match=None):
        """
        Return a union of all Eclipse Summary well names
        in all realizations (union). In addition, can return a list
        based on matches to an input string pattern.

        Args:
            well_match: `Optional`. String (or list of strings)
               with wildcard filter. If None, all wells are returned
        Returns:
            list of strings with eclipse well names. Empty list if no
            summary file or no matched well names.

        """
        if isinstance(well_match, str):
            well_match = [well_match]
        result = set()
        for _, realization in self._realizations.items():
            eclsum = realization.get_eclsum()
            if eclsum:
                if well_match is None:
                    result = result.union(set(eclsum.wells()))
                else:
                    for well in well_match:
                        result = result.union(set(eclsum.wells(well)))

        return sorted(list(result))

    def get_groupnames(self, group_match=None):
        """
        Return a union of all Eclipse Summary group names
        in all realizations (union).

        Optionally, the well names can be filtered.

        Args:
            well_match: `Optional`. String (or list of strings)
               with wildcard filter (globbing). If None, all
               wells are returned. Empty string does not match anything.
        Returns:
            list of strings with eclipse well names. Empty list if no
            summary file or no matched well names.

        """

        if isinstance(group_match, str):
            group_match = [group_match]
        result = set()
        for _, realization in self._realizations.items():
            eclsum = realization.get_eclsum()
            if eclsum:
                if group_match is None:
                    result = result.union(set(eclsum.groups()))
                else:
                    for group in group_match:
                        result = result.union(set(eclsum.groups(group)))

        return sorted(list(result))

    def agg(self, aggregation, keylist=None, excludekeys=None):
        """Aggregate the ensemble data into one VirtualRealization

        All data will be attempted aggregated. String data will typically
        be dropped in the result.

        Arguments:
            aggregation: string, supported modes are
                'mean', 'median', 'p10', 'p90', 'min',
                'max', 'std, 'var', 'pXX' where X is a number
            keylist: list of strings, indicating which keys
                in the internal datastore to include. If list is empty
                (default), all data will be attempted included.
            excludekeys: list of strings that should be excluded if
                keylist is empty, otherwise ignored
        Returns:
            VirtualRealization. Its name will include the aggregation operator

        WARNING: This code is duplicated in virtualensemble.py
        """
        quantilematcher = re.compile(r"p(\d\d)")
        supported_aggs = ["mean", "median", "min", "max", "std", "var"]
        if aggregation not in supported_aggs and not quantilematcher.match(aggregation):
            raise ValueError(
                "{arg} is not a".format(arg=aggregation)
                + "supported ensemble aggregation"
            )

        # Generate a new empty object:
        vreal = VirtualRealization(self.name + " " + aggregation)

        # Determine keys to use
        if isinstance(keylist, str):
            keylist = [keylist]
        if not keylist:  # Empty list means all keys.
            if not isinstance(excludekeys, list):
                excludekeys = [excludekeys]
            keys = set(self.keys()) - set(excludekeys)
        else:
            keys = keylist

        for key in keys:
            # Aggregate over this ensemble:
            # Ensure we operate on fully qualified localpath's
            key = self.shortcut2path(key)
            data = self.get_df(key)

            # This column should never appear in aggregated data
            del data["REAL"]

            # Look for data we should group by. This would be beneficial
            # to get from a metadata file, and not by pure guesswork.
            groupbycolumncandidates = [
                "DATE",
                "FIPNUM",
                "ZONE",
                "REGION",
                "JOBINDEX",
                "Zone",
                "Region_index",
            ]

            # Pick up string columns (or non-numeric values)
            # (when strings are used as values, this breaks, but it is also
            # meaningless to aggregate them. Most likely, strings in columns
            # is a label we should group over)
            stringcolumns = [x for x in data.columns if data.dtypes[x] == "object"]

            groupby = [x for x in groupbycolumncandidates if x in data.columns]

            # Add remainding string columns to columns to group by unless
            # we are working with the STATUS dataframe, which has too many strings..
            if key != "STATUS":
                groupby = list(set(groupby + stringcolumns))

            # Filter to only numerical columns and groupby columns:
            numerical_and_groupby_cols = list(
                set(list(groupby) + list(data.select_dtypes(include="number").columns))
            )
            data = data[numerical_and_groupby_cols]

            dtypes = data.dtypes.unique()
            if not (int in dtypes or float in dtypes):
                logger.info("No numerical data to aggregate in %s", key)
                continue
            if len(groupby):
                logger.info("Grouping %s by %s", key, groupby)
                aggobject = data.groupby(groupby)
            else:
                aggobject = data

            if quantilematcher.match(aggregation):
                quantile = int(quantilematcher.match(aggregation).group(1))
                aggregated = aggobject.quantile(quantile / 100.0)
            else:
                # Passing through the variable 'aggregation' to
                # Pandas, thus supporting more than we have listed in
                # the docstring.
                aggregated = aggobject.agg(aggregation)

            if groupby:
                aggregated.reset_index(inplace=True)

            # We have to recognize scalars.
            if len(aggregated) == 1 and aggregated.index.values[0] == key:
                aggregated = parse_number(aggregated.values[0])
            vreal.append(key, aggregated)
        return vreal

    @property
    def files(self):
        """Return a concatenation of files in each realization"""
        filedflist = []
        for realidx, realization in self._realizations.items():
            realfiles = realization.files.copy()
            realfiles.insert(0, "REAL", realidx)
            filedflist.append(realfiles)
        return pd.concat(filedflist, ignore_index=True, sort=False)

    @property
    def name(self):
        """The ensemble name."""
        return self._name

    @name.setter
    def name(self, newname):
        if isinstance(newname, str):
            self._name = newname
        else:
            raise ValueError("Name input is not a string")

    def __sub__(self, other):
        result = EnsembleCombination(ref=self, sub=other)
        return result

    def __add__(self, other):
        result = EnsembleCombination(ref=self, add=other)
        return result

    def __mul__(self, other):
        result = EnsembleCombination(ref=self, scale=float(other))
        return result

    def __rsub__(self, other):
        result = EnsembleCombination(ref=self, sub=other)
        return result

    def __radd__(self, other):
        result = EnsembleCombination(ref=self, add=other)
        return result

    def __rmul__(self, other):
        result = EnsembleCombination(ref=self, scale=float(other))
        return result

    def get_smry(
        self,
        time_index=None,
        column_keys=None,
        cache_eclsum=True,
        start_date=None,
        end_date=None,
        include_restart=True,
    ):
        """
        Aggregates summary data from all realizations.

        Wraps around Realization.get_smry() which wraps around
        ecl.summary.EclSum.pandas_frame()

        Args:
            time_index: list of DateTime if interpolation is wanted
               default is None, which returns the raw Eclipse report times
               If a string is supplied, that string is attempted used
               via get_smry_dates() in order to obtain a time index.
            column_keys: list of column key wildcards
            cache_eclsum: boolean for whether to cache the EclSum
                objects. Defaults to True. Set to False if
                not enough memory to keep all summary files in memory.
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
            A DataFame of summary vectors for the ensemble. The column
            REAL with integers is added to distinguish realizations. If
            no realizations, empty DataFrame is returned.
        """
        if isinstance(time_index, str):
            time_index = self.get_smry_dates(
                time_index,
                start_date=start_date,
                end_date=end_date,
                include_restart=include_restart,
            )
        dflist = []
        for index, realization in self._realizations.items():
            dframe = realization.get_smry(
                time_index=time_index,
                column_keys=column_keys,
                cache_eclsum=cache_eclsum,
                include_restart=include_restart,
            )
            dframe.insert(0, "REAL", index)
            dframe.index.name = "DATE"
            dflist.append(dframe)
        if dflist:
            return pd.concat(dflist, sort=False).reset_index()
        return pd.DataFrame()

    def get_eclgrid(self, props, report=0, agg="mean", active_only=False):
        """
        Returns the grid (i,j,k) and (x,y), and any requested init
        and/or unrst property. The values are aggregated over the
        ensemble (mean/ std currently supported).

        Args:
            props: list of column key wildcards
            report: int. for unrst props only. Report step for given date.
                    Use the function get_unrst_report_dates to get an overview
                    of the report steps availible.
            agg: String. "mean" or "std".
            active_only: bool. True if activate cells only.
        Returns:
            A dictionary. Index by grid attribute, and contains a list
            corresponding to a set of values for each grid cells.
        """
        ref = list(self._realizations.values())[0]
        grid_index = ref.get_grid_index(active_only=active_only)
        corners = ref.get_grid_corners(grid_index)
        centre = ref.get_grid_centre(grid_index)
        dframe = grid_index.reset_index().join(corners).join(centre)
        dframe["realizations_active"] = self.global_active.numpy_copy()
        for prop in props:
            print("Reading the grid property: " + prop)
            if prop in self.init_keys:
                dframe[prop] = self.get_init(prop, agg=agg)
            if prop in self.unrst_keys:
                dframe[prop] = self.get_unrst(prop, agg=agg, report=report)
        dframe.drop("index", axis=1, inplace=True)
        dframe.set_index(["i", "j", "k", "active"])
        return dframe

    @property
    def global_active(self):
        """
        :returns: An EclKw with, for each cell,
            the number of realizations where the cell is active.
        """
        if not self._global_active:
            self._global_active = EclKW(
                "eactive", self.global_size, EclDataType.ECL_INT
            )
            for realization in self._realizations.values():
                self._global_active += realization.actnum

        return self._global_active

    @property
    def global_size(self):
        """
        :returns: global size of the realizations in the Ensemble.  see
            :func:`fmu_postprocessing.modelling.Realization.global_size()`.
        """
        if not self._realizations:
            return 0
        if self._global_size is None:
            self._global_size = list(self._realizations.values())[0].global_size
        return self._global_size

    def _get_grid_index(self, active=True):
        """
        :returns: The grid of the ensemble, see
            :func:`fmu.ensemble.Realization.get_grid()`.
        """
        if not self._realizations:
            return None
        return list(self._realizations.values())[0].get_grid_index(active=active)

    @property
    def init_keys(self):
        """ Keys availible in the eclipse init file """
        if not self._realizations:
            return None
        all_keys = set.union(
            *[
                set(realization.get_init().keys())
                for _, realization in six.iteritems(self._realizations)
            ]
        )
        return all_keys

    @property
    def unrst_keys(self):
        """ Keys availaible in the eclipse unrst file """
        if not self._realizations:
            return None
        all_keys = set.union(
            *[
                set(realization.get_unrst().keys())
                for _, realization in six.iteritems(self._realizations)
            ]
        )
        return all_keys

    def get_unrst_report_dates(self):
        """ returns unrst report step and the corresponding date """
        if not self._realizations:
            return None
        all_report_dates = set.union(
            *[
                set(realization.report_dates)
                for _, realization in six.iteritems(self._realizations)
            ]
        )
        all_report_dates = list(all_report_dates)
        all_report_dates.sort()
        dframe = pd.DataFrame(all_report_dates, columns=["Dates"])
        dframe.index.names = ["Report"]
        return dframe

    def get_init(self, prop, agg):
        """
        :param prop: A time independent property,
        :returns: Dictionary with ``mean`` or ``std_dev`` as keys,
            and corresponding values for given property as values.
        :raises ValueError: If prop is not found.
        """
        if agg == "mean":
            mean = self._keyword_mean(prop, self.global_active)
            return pd.Series(mean.numpy_copy(), name=prop)
        if agg == "std":
            std_dev = self._keyword_std_dev(prop, self.global_active, mean)
            return pd.Series(std_dev.numpy_copy(), name=prop)

    def get_unrst(self, prop, report, agg):
        """
        :param prop: A time dependent property, see
            `fmu_postprocessing.modelling.SimulationGrid.TIME_DEPENDENT`.
        :returns: Dictionary with ``mean`` and ``std_dev`` as keys,
            and corresponding values for given property as values.
        :raises ValueError: If prop is not in `TIME_DEPENDENT`.
        """

        if agg == "mean":
            mean = self._keyword_mean(prop, self.global_active, report=report)
            return pd.Series(mean.numpy_copy(), name=prop)
        if agg == "std":
            std_dev = self._keyword_std_dev(
                prop, self.global_active, mean, report=report
            )
            return pd.Series(std_dev.numpy_copy(), name=prop)

    def _keyword_mean(self, prop, global_active, report=None):
        """
        :returns: Mean values of keywords.
        :param prop: Name of resulting Keyword.
        :param global_active: A EclKW with, for each cell, The number of
            realizations where the cell is active.
        :param report: Report step for unrst keywords
        """
        if report:
            mean = EclKW(prop, len(global_active), EclDataType.ECL_FLOAT)
            for real, realization in six.iteritems(self._realizations):
                mean += realization.get_global_unrst_keyword(prop, report)
            mean.safe_div(global_active)
            return mean
        else:
            mean = EclKW(prop, len(global_active), EclDataType.ECL_FLOAT)
            for _, realization in six.iteritems(self._realizations):
                mean += realization.get_global_init_keyword(prop)
            mean.safe_div(global_active)
            return mean

    def _keyword_std_dev(self, prop, global_active, mean, report=0):
        """
        :returns: Standard deviation of keywords.
        :param name: Name of resulting Keyword.
        :param keywords: List of pairs of keywords and list of active cell
        :param global_active: A EclKW with, for each cell, The number of
            realizations where the cell is active.
        :param mean: Mean of keywords.
        """
        if report:
            std_dev = EclKW(prop, len(global_active), EclDataType.ECL_FLOAT)
            for real, realization in six.iteritems(self._realizations):
                real_prop = realization.get_global_unrst_keyword(prop, report)
                std_dev.add_squared(real_prop - mean)
            std_dev.safe_div(global_active)
            return std_dev.isqrt()

        else:
            std_dev = EclKW(prop, len(global_active), EclDataType.ECL_FLOAT)
            for real, realization in six.iteritems(self._realizations):
                real_prop = realization.get_global_init_keyword(prop)
                std_dev.add_squared(real_prop - mean)
            std_dev.safe_div(global_active)
            return std_dev.isqrt()


def _convert_numeric_columns(dataframe):
    """Discovers and searches for numeric columns
    among string columns in an incoming dataframe.
    Columns with mostly integer

    Args:
        dataframe: any Pandas dataframe with strings as column datatypes

    Returns:
        A dataframe where some columns have had their datatypes
        converted to numerical types (int/float). Some values
        might contain numpy.nan.
    """
    logger.warning("_convert_numeric_columns() not implemented")
    return dataframe
