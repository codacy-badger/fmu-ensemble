# -*- coding: utf-8 -*-
"""Module for book-keeping and aggregation of ensembles
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import re
import os
import glob
import pandas as pd

from fmu.config import etc
from .ensemble import ScratchEnsemble, VirtualEnsemble

xfmu = etc.Interaction()
logger = xfmu.functionlogger(__name__)


class EnsembleSet(object):
    """An ensemble set is any collection of ensemble objects

    Ensemble objects are ScratchEnsembles or VirtualEnsembles.

    There is support for initializing from a filstructure with both
    iterations and batches, but the concept of iterations and batches
    are not kept in an EnsembleSet, there each ensemble is uniquely
    identified by the ensemble name. To keep the iteration (and batch)
    concept, that must be embedded into the ensemble name.
    """

    def __init__(self, name=None, ensembles=None, frompath=None,
                 realidxregexp=None, iterregexp=None, batchregexp=None):
        """Initiate an ensemble set, either as empty, or from
        a list of already initialized ensembles, or directly from the
        filesystem.

        Args:
        name: Chosen name for the ensemble set. Can be used if aggregated at a
            higher level.
        ensembles: list of Ensemble objects. Can be omitted.
        frompath: string or list of strings with filesystem path.
            Will be globbed by default. If no realizations or iterations
            are detected after globbing, the standard glob
            'realization-*/iter-*/ will be used.
        realidxregexp: regular expression object that will be used to
            determine the realization index (must be integer) from a path
            component (split by /). The default fits realization-*
        iterregexp: similar to realidxregexp, and result will always be
            treated as a string.
        batchregexp: similar ot iterregexp, for future support of an extra
            level similar to iterations
        """
        self._name = name
        self._ensembles = {}  # Dictionary indexed by each ensemble's name.

        if ensembles and frompath:
            logger.error("EnsembleSet cannot initialize from " +
                         "both list of ensembles and path")

        # Check consistency in arguments.
        if not name:
            logger.error("Name of EnsembleSet is required")
            return None
        if name and not isinstance(name, str):
            logger.error("Name of EnsembleSet must be a string")
            return None
        if frompath and not isinstance(frompath, str):
            logger.error("frompath arg given to EnsembleSet must be a string")
            return None
        if ensembles and not isinstance(ensembles, list):
            logger.error("Ensembles supplied to EnsembleSet must be a list")
            return None

        if ensembles and isinstance(ensembles, list):
            for ensemble in ensembles:
                if isinstance(ensemble, (ScratchEnsemble, VirtualEnsemble)):
                    self._ensembles[ensemble.name] = ensemble
                else:
                    logger.warning("Supplied object was not an ensemble")
            if not self._ensembles:
                logger.warning("No ensembles added to EnsembleSet")

        if frompath:
            self.add_ensembles_frompath(frompath, realidxregexp,
                                        iterregexp, batchregexp)
            if not self._ensembles:
                logger.warning("No ensembles added to EnsembleSet")

    @property
    def name(self):
        """Return the name of the ensembleset,
        as initialized"""
        return self._name

    def __len__(self):
        return len(self._ensembles)

    def __getitem__(self, name):
        return self._ensembles[name]

    def __repr__(self):
        return "<EnsembleSet {}, {} ensembles:\n{}>".format(
            self.name, len(self), self._ensembles)

    @property
    def ensemblenames(self):
        """
        Return a list of named ensembles in this set
        """
        return self._ensembles.keys()

    def keys(self):
        """
        Return the union of all keys available in the ensembles.

        Keys refer to the realization datastore, a dictionary
        of dataframes or dicts.
        """
        allkeys = set()
        for ensemble in self._ensembles.values():
            allkeys = allkeys.union(ensemble.keys())
        return allkeys

    def add_ensembles_frompath(self, paths,
                               realidxregexp=None, iterregexp=None,
                               batchregexp=None):
        """Convenience function for adding multiple ensembles.

        Args:
            paths: str or list of strings with path to the
                directory containing the realization-*/iter-*
                structure
            realidxregexp: Supply a regexp that can extract the realization
                index as an *integer* from path components.
                The expression will be tested on individual path
                components from right to left.
            iterregexp: Similar to real_regexp, but is allowed to
                match strings.
            batchregexp: Similar to real_regexp, but is allowed to
                match strings.
        """
        # Try to catch the most common use case and make that easy:
        if isinstance(paths, str):
            if 'realization' not in paths and not realidxregexp\
               and not iterregexp and not batchregexp:
                logger.info("Adding realization-*/iter-* "
                            + "path pattern to case directory")
                paths = paths + '/realization-*/iter-*'
            paths = [paths]

        if not realidxregexp:
            realidxregexp = re.compile(r'realization-(\d+)')
        if isinstance(realidxregexp, str):
            realidxregexp = re.compile(realidxregexp)
        if not iterregexp:
            # Alternative regexp that extracts iteration
            # as an integer
            # iterregexp = re.compile(r'iter-(\d+)')
            # Default regexp that will add 'iter-' to the
            # ensemble name
            iterregexp = re.compile(r'(iter-\d+)')
        if isinstance(iterregexp, str):
            iterregexp = re.compile(iterregexp)
        if not batchregexp:
            batchregexp = re.compile(r'batch-(\d+)')
        if isinstance(batchregexp, str):
            batchregexp = re.compile(batchregexp)

        # Check that the regexpes actually can return something
        if realidxregexp.groups != 1:
            logger.critical("Invalid regular expression for realization")
            return
        if iterregexp.groups != 1:
            logger.critical("Invalid regular expression for iter")
            return
        if batchregexp.groups != 1:
            logger.critical("Invalid regular expression for batch")
            return

        globbedpaths = [glob.glob(path) for path in paths]
        globbedpaths = list(set([item for sublist in globbedpaths
                                 for item in sublist]))

        # Build a temporary dataframe of globbed paths, and columns with
        # the realization index and the iter we found
        # (extented to a third level called 'batch')
        paths_df = pd.DataFrame(columns=['path', 'real', 'iter', 'batch'])
        for path in globbedpaths:
            real = None
            iter = None
            batch = None
            for path_comp in reversed(path.split(os.path.sep)):
                realmatch = re.match(realidxregexp, path_comp)
                if realmatch:
                    real = int(realmatch.group(1))
                    break
            for path_comp in reversed(path.split(os.path.sep)):
                itermatch = re.match(iterregexp, path_comp)
                if itermatch:
                    iter = str(itermatch.group(1))
                    break
            for path_comp in reversed(path.split(os.path.sep)):
                batchmatch = re.match(batchregexp, path_comp)
                if batchmatch:
                    batch = str(itermatch.group(1))
                    break
            df_row = {'path': path,
                      'real': real,
                      'iter': iter,
                      'batch': batch}
            paths_df = paths_df.append(df_row, ignore_index=True)

        # Initialize ensemble objects for each iter found:
        iters = sorted(paths_df['iter'].unique())
        logger.info("Identified %s iterations, %s", len(iters), iters)
        for iterr in iters:
            pathsforiter = sorted(paths_df[paths_df['iter'] == iterr]
                                  ['path'].values)
            # iterr might contain the 'iter-' prefix,
            # depending on chosen regexpx
            ens = ScratchEnsemble(str(iterr),
                                  pathsforiter, realidxregexp=realidxregexp)
            self._ensembles[ens.name] = ens

    def add_ensemble(self, ensembleobject):
        """Add a single ensemble to the ensemble set

        Name is taken from the ensembleobject.
        """
        if ensembleobject.name in self._ensembles:
            raise ValueError("The name %s already exists in the EnsembleSet",
                             ensembleobject.name)
        self._ensembles[ensembleobject.name] = ensembleobject

    @property
    def parameters(self):
        """Getter for ensemble.parameters(convert_numeric=True)
        """
        return self.get_df('parameters.txt')

    def load_scalar(self, localpath, convert_numeric=False,
                    force_reread=False):
        """Parse a single value from a file

        The value can be a string or a number. Empty files
        are treated as existing, with an empty string as
        the value, different from non-existing files.

        Parsing is performed individually in each ensemble
        and realization"""
        for ensname, ensemble in self._ensembles.items():
            try:
                ensemble.load_scalar(localpath, convert_numeric,
                                     force_reread)
            except ValueError:
                # This will occur if an ensemble is missing the file.
                # At ensemble level that is an Error, but at EnsembleSet level
                # it is only a warning.
                logger.warn('Ensemble %s did not contain the data %s', ensname,
                            localpath)

    def load_txt(self, localpath, convert_numeric=True,
                 force_reread=False):
        """Parse and internalize a txt-file from disk

        Parses text files on the form
        <key> <value>
        in each line."""
        return self.load_file(localpath, 'txt', convert_numeric,
                              force_reread)

    def load_csv(self, localpath, convert_numeric=True,
                 force_reread=False):
        """Parse and internalize a CSV file from disk"""
        return self.load_file(localpath, 'csv', convert_numeric,
                              force_reread)

    def load_file(self, localpath, fformat, convert_numeric=True,
                  force_reread=False):
        """Internal function for load_*()"""
        for ensname, ensemble in self._ensembles.items():
            try:
                ensemble.load_file(localpath, fformat, convert_numeric,
                                   force_reread)
            except ValueError:
                # This will occur if an ensemble is missing the file.
                # At ensemble level that is an Error, but at EnsembleSet level
                # it is only a warning.
                logger.warn('Ensemble %s did not contain the data %s', ensname,
                            localpath)
        return self.get_df(localpath)

    def get_df(self, localpath):
        """Collect contents of dataframes from each ensemble

        Args:
            localpath: path to the text file, relative to each realization
            convert_numeric: If set to True, numerical columns
                will be searched for and have their dtype set
                to integers or floats.
            force_reread: Force reread from file system. If
                False, repeated calls to this function will
                returned cached results.
        """
        ensdflist = []
        for ensname, ensemble in self._ensembles.items():
            try:
                ensdf = ensemble.get_df(localpath)
                ensdf.insert(0, 'ENSEMBLE', ensemble.name)
                ensdflist.append(ensdf)
            except ValueError:
                # Happens if an ensemble is missing some data
                # Warning has already been issued at initialization
                pass
        if ensdflist:
            return pd.concat(ensdflist, sort=False)
        else:
            raise ValueError("No data found for %s", localpath)

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
        for _, ensemble in self._ensembles.items():
            try:
                ensemble.drop(localpath, **kwargs)
            except ValueError:
                pass  # Allow localpath to be missing in some ensembles.

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

        CODE DUPLICATION from realization.py
        """
        basenames = map(os.path.basename, self.keys())
        if basenames.count(shortpath) == 1:
            short2path = {os.path.basename(x): x for x in self.keys()}
            return short2path[shortpath]
        noexts = [''.join(x.split('.')[:-1]) for x in self.keys()]
        if noexts.count(shortpath) == 1:
            short2path = {''.join(x.split('.')[:-1]): x
                          for x in self.keys()}
            return short2path[shortpath]
        basenamenoexts = [''.join(os.path.basename(x).split('.')[:-1])
                          for x in self.keys()]
        if basenamenoexts.count(shortpath) == 1:
            short2path = {''.join(os.path.basename(x).split('.')[:-1]): x
                          for x in self.keys()}
            return short2path[shortpath]
        # If we get here, we did not find anything that
        # this shorthand could point to. Return as is, and let the
        # calling function handle further errors.
        return shortpath

    def get_csv_deprecated(self, filename):
        """Load CSV data from each realization in each
        ensemble, and aggregate.

        Args:
            filename: string, filename local to realization
        Returns:
            dataframe: Merged CSV from each realization.
                Realizations with missing data are ignored.
                Empty dataframe if no data is found
        """
        dflist = []
        for _, ensemble in self._ensembles.items():
            dframe = ensemble.get_csv(filename)
            dframe['ENSEMBLE'] = ensemble.name
            dflist.append(dframe)
        return pd.concat(dflist, sort=False)

    def load_smry(self, time_index=None, column_keys=None):
        """
        Fetch summary data from all ensembles

        Wraps around Ensemble.load_smry() which wraps
        Realization.load_smry(), which wraps ecl.summary.EclSum.pandas_frame()

        The time index is determined at realization level. If you
        ask for 'monthly', you will from each realization get its
        months. At ensemble or ensembleset-level, the number of
        monthly report dates between realization can vary

        The pr. realization results will be cached by each
        realization object, and can be retrieved through get_df().

        Args:
            time_index: list of DateTime if interpolation is wanted
               default is None, which returns the raw Eclipse report times
               If a string is supplied, that string is attempted used
               via get_smry_dates() in order to obtain a time index.
            column_keys: list of column key wildcards
        Returns:
            A DataFame of summary vectors for the ensembleset.
            The column 'ENSEMBLE' will denote each ensemble's name
        """
        # Future: Multithread this:
        for _, ensemble in self._ensembles.items():
            ensemble.load_smry(time_index=time_index,
                               column_keys=column_keys)
        if isinstance(time_index, list):
            time_index = 'custom'
        return self.get_df('share/results/tables/unsmry--' +
                           time_index + '.csv')

    def get_smry_dates(self, freq='monthly'):
        """Return list of datetimes from an ensembleset

        Datetimes from each realization in each ensemble can
        be returned raw, or be resampled.

        Args:
           freq: string denoting requested frequency for
               the returned list of datetime. 'report' will
               yield the sorted union of all valid timesteps for
               all realizations. Other valid options are
               'daily', 'monthly' and 'yearly'.
        Returns:
            list of datetime.date.
        """

        rawdates = set()
        for _, ensemble in self._ensembles.items():
            rawdates = rawdates.union(ensemble.get_smry_dates(freq='report'))
        rawdates = list(rawdates)
        rawdates.sort()
        if freq == 'report':
            return rawdates
        else:
            # Later optimization: Wrap eclsum.start_date in the
            # ensemble object.
            start_date = min(rawdates)
            end_date = max(rawdates)
            pd_freq_mnenomics = {'monthly': 'MS',
                                 'yearly': 'YS', 'daily': 'D'}
            if freq not in pd_freq_mnenomics:
                raise ValueError('Requested frequency %s not supported' % freq)
            datetimes = pd.date_range(start_date, end_date,
                                      freq=pd_freq_mnenomics[freq])
            # Convert from Pandas' datetime64 to datetime.date:
            return [x.date() for x in datetimes]
