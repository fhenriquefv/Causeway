""" Define standard machine-learned model framework for pipelines. """

from gflags import DEFINE_bool, FLAGS, DuplicateFlagError, UnrecognizedFlag
import itertools
import logging
import numpy as np
import pycrfsuite
from scipy.sparse import lil_matrix, vstack
import time
from types import MethodType

from pipeline.feature_extractors import FeatureExtractor, FeatureExtractionError
from util import NameDictionary
# from util.metrics import diff_binary_vectors

try:
    DEFINE_bool(
        'rebalance_stochastically', False,
        'Rebalance classes by stochastically choosing samples to replicate')
    DEFINE_bool('pycrfsuite_verbose', False,
                'Verbose logging output from python-crfsuite trainer')
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)


class Model(object):
    def __init__(self, part_type):
        self.part_type = part_type

    def train(self, parts):
        raise NotImplementedError

    def test(self, parts):
        raise NotImplementedError

    def save(self, filepath):
        raise NotImplementedError

    def load(self, filepath):
        raise NotImplementedError


class FeaturizedModel(Model):
    class ConjoinedFeatureExtractor(FeatureExtractor):
        '''
        A FeaturizedModel allows combining features. This class provides the
        necessary functionality of combining the FeatureExtractors' outputs.
        '''
        SEPARATOR = ':'

        def __init__(self, name, extractors):
            self.name = name
            self.feature_type = self.FeatureTypes.Categorical
            for extractor in extractors:
                if extractor.feature_type != self.FeatureTypes.Categorical:
                    raise FeatureExtractionError(
                        "Only categorical features can be conjoined (attempted"
                        " to conjoin %s)" % [e.name for e in extractors])
            self._extractors = extractors

        def extract_subfeature_names(self, parts):
            subfeature_name_components = [
                extractor.extract_subfeature_names(parts)
                for extractor in self._extractors]
            return [self.SEPARATOR.join(components) for components
                    in itertools.product(*subfeature_name_components)]

        def train(self, parts):
            for extractor in self._extractors:
                extractor.train(parts)

        def extract(self, part):
            return self.SEPARATOR.join([extractor.extract(part)
                                        for extractor in self._extractors])

    def __init__(self, part_type, feature_extractors, selected_features,
                 save_featurized=False):
        """
        part_type is the class object corresponding to the part type this model
            is for.
        feature_extractors is a list of
            `pipeline.feature_extractors.FeatureExtractor` objects.
        selected_features is a list of names of features to extract. Names may
            be combinations of feature names, separated by ':'.
        save_featurized indicates whether to store features and labels
            properties after featurization. Useful for debugging/development.
        """
        super(FeaturizedModel, self).__init__(part_type)
        self.feature_name_dictionary = NameDictionary()

        self.feature_extractors = []
        for feature_name in selected_features:
            extractor_names = feature_name.split(
                self.ConjoinedFeatureExtractor.SEPARATOR)
            if len(extractor_names) > 1:
                extractors = [extractor for extractor in feature_extractors
                              if extractor.name in extractor_names]
                if len(extractors) != len(extractor_names):
                    raise UnrecognizedFlag("Invalid conjoined feature name: %s"
                                           % feature_name)
                self.feature_extractors.append(
                    self.ConjoinedFeatureExtractor(feature_name, extractors))
            else:
                try:
                    # Find the correct extractor with a generator, which will
                    # stop searching once it's found.
                    extractor_generator = (
                        extractor for extractor in feature_extractors
                        if extractor.name == feature_name)
                    extractor = extractor_generator.next()
                    self.feature_extractors.append(extractor)
                except StopIteration:
                    raise UnrecognizedFlag("Invalid feature name: %s"
                                           % feature_name)

        self.feature_training_data = None
        self.save_featurized = save_featurized

    def train(self, parts):
        # Reset state in case we've been previously trained.
        self.feature_name_dictionary.clear()
        self.feature_training_data = None

        # Build feature name dictionary. (Unfortunately, this means we
        # featurize many things twice, but I can't think of a cleverer way to
        # discover the possible values of a feature.)
        logging.info("Registering features...")

        for extractor in self.feature_extractors:
            logging.debug('Registering feature "%s"' % extractor.name)
            self.feature_training_data = extractor.train(parts)
            subfeature_names = extractor.extract_subfeature_names(parts)
            for subfeature_name in subfeature_names:
                self.feature_name_dictionary.insert(subfeature_name)
            logging.debug("%d features registered in map for '%s'" % (
                            len(subfeature_names), extractor.name))

        logging.info('Done registering features.')

        self._featurized_train(parts)

    def test(self, parts):
        assert self.feature_name_dictionary, (
            "Feature name dictionary must be populated either by training or by"
            " loading a model")
        self._featurized_test(parts)

    def _featurized_train(self, parts):
        raise NotImplementedError

    def _featurized_test(self, parts):
        raise NotImplementedError


class ClassifierModel(FeaturizedModel):
    def __init__(self, part_type, feature_extractors, selected_features,
                 classifier):
        """
        Note that classifier must support the fit and predict methods in the
        style of scikit-learn.
        """
        super(ClassifierModel, self).__init__(part_type, feature_extractors,
                                              selected_features)
        self.classifier = classifier

    def _featurized_train(self, parts):
        features, labels = self._featurize(parts)
        logging.info('Fitting classifier...')
        self.classifier.fit(features, labels)
        logging.info('Done fitting classifier.')

    def _featurized_test(self, parts):
        features, gold_labels = self._featurize(parts)
        if self.save_featurized:
            self.gold_labels = gold_labels
        labels = self.classifier.predict(features)
        for part, label in zip(parts, labels):
            part.label = label
        # logging.debug('%d data points' % len(gold_labels))
        # logging.debug('Raw classifier performance:')
        # logging.debug('\n' + str(diff_binary_vectors(labels, gold_labels)))

    def _featurize(self, parts):
        logging.info('Featurizing...')
        start_time = time.time()

        relevant_parts = [part for part in parts if isinstance(part,
                                                               self.part_type)]
        features = lil_matrix(
            (len(relevant_parts), len(self.feature_name_dictionary)),
            dtype=np.float32) # TODO: Make this configurable?
        labels = np.fromiter((part.label for part in relevant_parts),
                             int, len(relevant_parts))

        for extractor in self.feature_extractors:
            feature_values_by_part = extractor.extract_all(parts)
            for part_index, part_subfeature_values in enumerate(
                feature_values_by_part):
                for subfeature_name, subfeature_value in (
                    part_subfeature_values.iteritems()):
                    if subfeature_value == 0:
                        continue # Don't bother setting 0's in a sparse matrix.
                    try:
                        feature_index = self.feature_name_dictionary[
                            subfeature_name]
                        features[part_index, feature_index] = subfeature_value
                    except KeyError:
                        logging.debug('Ignoring unknown subfeature: %s'
                                      % subfeature_name)

        features = features.tocsr()
        elapsed_seconds = time.time() - start_time
        logging.info('Done featurizing in %0.2f seconds' % elapsed_seconds)
        if self.save_featurized:
            self.features = features
            self.labels = labels
        return features, labels


class ClassifierPart(object):
    def __init__(self, instance, label):
        self.instance = instance
        self.label = int(label)


class ClassBalancingModelWrapper(object):
    def __init__(self, classifier, ratio=float('inf')):
        self.classifier = classifier
        self.ratio = ratio

    @staticmethod
    def rebalance(data, labels, ratio=float('inf')):
        """
        data is a sparse matrix; labels is array-like.
        ratio indicates the maximum ratio of its current count to which any
        class is allowed to increase.
        """
        if ratio <= 1.0: # No increase
            return data, labels

        # Based on http://stackoverflow.com/a/23392678/4044809
        label_set, label_indices, label_counts = np.unique(
            labels, return_inverse=True, return_counts=True)

        max_count = label_counts.max()
        counts_to_add = [
            # -1 adjusts for the current_count we already have
            int(min(max_count - current_count, (ratio - 1) * current_count))
            for current_count in label_counts]
        rows_to_add = np.sum(counts_to_add)
        if rows_to_add == 0:
            # This is essential not just for efficiency, but because scipy's
            # vstack apparently doesn't know how to vstack a 0-row matrix.
            return data, labels

        # Use lil_matrix to support slicing.
        rebalanced_data = lil_matrix((rows_to_add, data.shape[1]),
                                     dtype=data.dtype)
        rebalanced_labels = np.empty((rows_to_add,), labels.dtype)

        slices = np.concatenate(([0], np.cumsum(counts_to_add)))
        for j in xrange(len(label_set)):
            label_row_indices = np.where(label_indices==j)[0]
            if FLAGS.rebalance_stochastically:
                indices = np.random.choice(label_row_indices,
                                           counts_to_add[j])
            else:
                full_repetitions = (
                    counts_to_add[j] / label_row_indices.shape[0])
                logging.debug("Increasing count for label %d by %d+"
                              % (j, full_repetitions))
                indices = np.tile(label_row_indices, (full_repetitions,))
                still_needed = counts_to_add[j] - indices.shape[0]
                if still_needed:
                    indices = np.concatenate(
                        (indices, label_row_indices[:still_needed]))

            # Only bother to actually rebalance if there are changes to be made
            if indices.shape[0]:
                rebalanced_data[slices[j]:slices[j+1]] = data[indices]
                rebalanced_labels[slices[j]:slices[j+1]] = labels[indices]
        rebalanced_data = vstack((data, rebalanced_data))
        rebalanced_labels = np.concatenate((labels, rebalanced_labels))
        return (rebalanced_data, rebalanced_labels)

    def fit(self, data, labels):
        rebalanced_data, rebalanced_labels = self.rebalance(
            data, labels, self.ratio)
        return self.classifier.fit(rebalanced_data, rebalanced_labels)

    def predict(self, data):
        return self.classifier.predict(data)

class CRFModel(Model):
    # Theoretically, this class ought to be a type of FeaturizedModel. But that
    # class assumes we're doing all the feature management in class, and in
    # practice we're offloading CRF feature management to CRFSuite.

    class CRFTrainingError(Exception):
        pass

    class ObservationWithContext(object):
        '''
        In a CRF model, the feature extractors will have to operate not just on
        a single part (which may have many observations), but on each
        observation, *with* the context of the surrounding observations. This
        class encapsulates the data a CRF feature extractor may need.
        '''
        def __init__(self, observation, sequence, index, part):
            self.observation = observation
            self.sequence = sequence
            self.index = index
            self.part = part

    def __init__(self, part_type, model_file_path, feature_extractors,
                 selected_features, training_algorithm, training_params,
                 save_model_info=False):
        super(CRFModel, self).__init__(part_type)
        self.model_file_path = model_file_path
        self.feature_extractors = [extractor for extractor in feature_extractors
                                   if extractor.name in selected_features]
        self.training_algorithm = training_algorithm
        self.training_params = training_params
        self.save_model_info = save_model_info
        self.model_info = None

    def _sequences_for_part(self, part, is_train):
        '''
        Returns the observation and label sequences for the part. Should return
        None for labels at test time.
        '''
        raise NotImplementedError

    def _label_part(self, part, crf_labels):
        '''
        Applies the labels to the part as appropriate. Must be overridden.
        (In many cases with CRFs, this will involve interpreting the labels
        as spans.)
        '''
        raise NotImplementedError

    def __featurize_observation_sequence(self, observation_sequence, part):
        observation_features = []
        for i, observation in enumerate(observation_sequence):
            feature_values = {}
            for feature_extractor in self.feature_extractors:
                extractor_arg = self.ObservationWithContext(
                    observation, observation_sequence, i, part)
                feature_values.update(feature_extractor.extract(extractor_arg))
            observation_features.append(feature_values)
        return observation_features

    @staticmethod
    def __handle_training_error(trainer, log):
        raise CRFModel.CRFTrainingError('CRF training failed: %s' % log)

    def train(self, parts):
        trainer = pycrfsuite.Trainer(verbose=FLAGS.pycrfsuite_verbose)
        trainer.select(self.training_algorithm)
        trainer.set_params(self.training_params)
        error_handler = MethodType(self.__handle_training_error, trainer)
        trainer.on_prepare_error = error_handler

        for part in parts:
            observation_sequence, labels = self._sequences_for_part(part, True)
            observation_features = self.__featurize_observation_sequence(
                observation_sequence, part)
            trainer.append(observation_features, labels)

        start_time = time.time()
        logging.info("Training CRF model...")
        trainer.train(self.model_file_path)
        elapsed_seconds = time.time() - start_time
        logging.info('CRF model saved to %s (training took %0.2f seconds)'
                     % (self.model_file_path, elapsed_seconds))
        if self.save_model_info:
            tagger = pycrfsuite.Tagger()
            tagger.open(self.model_file_path)
            self.model_info = tagger.info()
            tagger.close()

    def test(self, parts):
        tagger = pycrfsuite.Tagger()
        tagger.open(self.model_file_path)
        for part in parts:
            observation_sequence, _ = self._sequences_for_part(part, False)
            observation_features = self.__featurize_observation_sequence(
                observation_sequence, part)
            crf_labels = tagger.tag(observation_features)
            self._label_part(part, crf_labels)
        tagger.close()
