from collections import defaultdict
from gflags import UnrecognizedFlag
from mock import MagicMock
import numpy as np
from scipy.sparse import lil_matrix, vstack
import unittest

from pipeline.feature_extractors import FeatureExtractor
from pipeline.models import ClassBalancingModelWrapper, FeaturizedModel


class SmallClassBalancingTest(unittest.TestCase):
    ZERO_ROW_VALS = [7, 8, 9]

    def setUp(self):
        self.data = lil_matrix(
            [[1, 2, 3],
             [4, 5, 6],
             SmallClassBalancingTest.ZERO_ROW_VALS,
             [10, 11, 12]], dtype='float')
        self.labels = np.array([1, 1, 0, 1])

    def test_unchanged_for_low_ratios(self):
        for ratio in [0.5, 1.0]:
            data, labels = ClassBalancingModelWrapper.rebalance(
                self.data, self.labels, ratio)
            self.assertEqual(data.shape, self.data.shape)
            self.assertEqual(labels.shape, self.labels.shape)

            self.assertTrue((data == self.data).toarray().all())
            self.assertTrue((labels == self.labels).all())

    @staticmethod
    def as_list_of_lists(sparse_matrix):
        return [[x for x in y] for y in sparse_matrix.toarray()]

    def _test_for_count(self, data, labels, count):
        # NOTE: This method relies on the values in the data matrix being
        # unique, or at least having consistent labels for row values.
        original_labels_by_row_val = {}
        for row, label in zip(self.as_list_of_lists(self.data), labels):
            original_labels_by_row_val[tuple(row)] = label

        counts_by_row_val = defaultdict(int)
        for row, label in zip(self.as_list_of_lists(data), labels):
            row = tuple(row)
            original_label = original_labels_by_row_val[row]
            self.assertEqual(original_label, label,
                             "Row %s with label %d has gotten label %d"
                                % (list(row), original_label, label))
            counts_by_row_val[row] += 1

        self.assertEqual(counts_by_row_val[(1.,2.,3.)], 1)
        self.assertEqual(counts_by_row_val[(4.,5.,6.)], 1)
        self.assertEqual(counts_by_row_val[tuple(self.ZERO_ROW_VALS)], count)
        self.assertEqual(counts_by_row_val[(10.,11.,12.)], 1)

    def test_unlimited_balancing(self):
        data, labels = ClassBalancingModelWrapper.rebalance(
            self.data, self.labels)
        self._test_for_count(data, labels, 3)

    def test_normal_balancing(self):
        for ratio in [2.0, 3.0]:
            data, labels = ClassBalancingModelWrapper.rebalance(
                self.data, self.labels, ratio)
            self._test_for_count(data, labels, int(ratio))

    def test_fractional_balancing(self):
        for ratio in [1.7, 2.2]:
            data, labels = ClassBalancingModelWrapper.rebalance(
                self.data, self.labels, ratio)
            self._test_for_count(data, labels, int(ratio))

    def test_balancing_with_uneven_multiples(self):
        # Add two new 0 rows, and 5 new 1 rows. Now the existing ratio of
        # labels is 3:8. This will force rebalancing to have 2 leftover rows
        # when it tries doing full replications. This makes this a good test
        # case for the regression of making the difference negative.
        self.data = vstack(
            [self.data, self.ZERO_ROW_VALS, self.ZERO_ROW_VALS]
                + ([[13, 13, 13]] * 5),
            format='lil')
        self.labels = np.append(self.labels, [0, 0] + [1] * 5)
        data, labels = ClassBalancingModelWrapper.rebalance(
            self.data, self.labels, 10)
        # Rebalancing should have added 5 copies of the zero row, for a total of 8.
        self._test_for_count(data, labels, 8)


class FeaturizedModelTest(unittest.TestCase):
    def setUp(self):
        self.identity_extractor = FeatureExtractor(
            'identity', lambda x: x, FeatureExtractor.FeatureTypes.Categorical)
        self.add1_extractor = FeatureExtractor(
            'add1', lambda x: x + 1, FeatureExtractor.FeatureTypes.Categorical)
        self.model = FeaturizedModel(
            int, [self.identity_extractor, self.add1_extractor],
            ['identity', 'add1', 'identity:add1'])
        self.model._featurized_train = MagicMock(return_value=None)

    def test_subfeature_names(self):
        self.model.train([1, 2])
        subfeature_names = set(
            self.model.feature_name_dictionary.names_to_ids.keys())
        correct = set(['identity=1', 'identity=2', 'add1=2', 'add1=3',
                       'identity=1:add1=2', 'identity=2:add1=3',
                       'identity=2:add1=2', 'identity=1:add1=3'])
        self.assertSetEqual(correct, subfeature_names)

    def test_complains_for_invalid_feature_names(self):
        def set_invalid_feature():
            self.model = FeaturizedModel(int, [self.identity_extractor],
                                         ['add1'])
        self.assertRaises(UnrecognizedFlag, set_invalid_feature)

        def set_invalid_combination():
            self.model = FeaturizedModel(int, [self.identity_extractor],
                                         ['identity:add1'])
        self.assertRaises(UnrecognizedFlag, set_invalid_combination)
