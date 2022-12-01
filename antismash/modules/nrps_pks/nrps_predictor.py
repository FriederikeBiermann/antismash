# License: GNU Affero General Public License v3 or later
# A copy of GNU AGPL v3 should have been included in this software package in LICENSE.txt.

""" Provides a collection of functions and classes to run the external Java program
    NRPSPredictor2 and interpret the results
"""

from collections import defaultdict
import os
import sys
from typing import Any, Dict, List, Set

from helperlibs.wrappers.io import TemporaryDirectory

from antismash.common import path, subprocessing
from antismash.common.html_renderer import Markup
from antismash.config import ConfigType
from antismash.detection.nrps_pks_domains import ModularDomain

from .data_structures import Prediction
from .signatures import get_a_dom_signatures

def _build_stach_codes() -> Dict[str, Set[str]]:
    """ Builds a mapping of Stachelhaus prediction to code from NRPSPredictor2's
        data for use in checking how good a hit it really is
    """
    data_file = path.get_full_path(__file__, "external/NRPSPredictor2/data/labeled_sigs")
    results: Dict[str, Set[str]] = defaultdict(set)
    with open(data_file) as handle:
        for line in handle:
            # in the form: prediction angstrom_code stach_code
            # with the stach code's 8th character always being '-'
            pred, _, stach = line.strip().rsplit(maxsplit=2)
            assert len(stach) == 10, "malformed NRPSPredictor data file"
            results[pred].add(stach)
    return results


KNOWN_STACH_CODES = _build_stach_codes()


class PredictorSVMResult(Prediction):
    """ Holds all the relevant results from NRPSPredictor2 for a domain """
    def __init__(self, angstrom_code: str, physicochemical_class: str, large_cluster_pred: List[str],
                 small_cluster_pred: List[str], single_amino_pred: str, stachelhaus_predictions: List[str],
                 uncertain: bool, stachelhaus_seq: str, stachelhaus_match_count: int) -> None:
        super().__init__("NRPSPredictor2")
        self.angstrom_code = str(angstrom_code)
        self.physicochemical_class = str(physicochemical_class)
        self.large_cluster_pred = list(large_cluster_pred)
        self.small_cluster_pred = list(small_cluster_pred)
        self.single_amino_pred = str(single_amino_pred)
        assert ',' not in self.single_amino_pred
        self.stachelhaus_predictions = list(stachelhaus_predictions)
        for pred in stachelhaus_predictions:
            assert '/' not in pred
        self.uncertain = bool(uncertain)
        self.stachelhaus_seq = str(stachelhaus_seq)
        self.stachelhaus_match_count = int(stachelhaus_match_count)

    def _get_classification(self) -> List[str]:
        # comparing number of stach matches (n) to which category of SVM prediction
        # was made, and also to whether the SVM registered being outside of applicability domain
        # < = take stach, ^ = take SVM, & = take intersection of both, . = neither
        #    n   | single small/large/physico outside
        #    10      <            <              <
        #     9      &            &              <
        #     8      ^            &              <
        #  <= 7      ^            ^              .
        classification: List[str] = []

        if self.uncertain:
            if self.stachelhaus_match_count >= 8:
                classification.extend(self.stachelhaus_predictions)
            return classification

        def stach_intersection_with_best_group() -> Set[str]:
            """ Finds the intersection of stach predictions with the tightest
                group of SVM predictions. If no SVM prediction, returns stach preds instead.
            """
            stach_preds = set(self.stachelhaus_predictions)
            for group in [self.small_cluster_pred, self.large_cluster_pred, [self.physicochemical_class]]:
                if group == ["N/A"]:
                    continue
                return stach_preds.intersection(set(group))
            return stach_preds

        stach_preds = set(self.stachelhaus_predictions)
        if self.stachelhaus_match_count == 10:
            classification.extend(sorted(stach_preds))
        elif self.single_amino_pred != "N/A":
            if self.stachelhaus_match_count == 9:
                if self.single_amino_pred in self.stachelhaus_predictions:
                    classification.append(self.single_amino_pred)
            else:
                classification.append(self.single_amino_pred)
        elif self.stachelhaus_match_count >= 8:
            classification.extend(stach_intersection_with_best_group())
        else:  # < 8 and not uncertain
            for group in [[self.single_amino_pred], self.small_cluster_pred,
                          self.large_cluster_pred, [self.physicochemical_class]]:
                if group != ["N/A"]:
                    classification.extend(group)
                    break
        return classification

    def get_classification(self) -> List[str]:
        return list(map(map_nrpspredicor_to_norine, self._get_classification()))

    def as_html(self) -> Markup:
        note = ""
        if self.uncertain:
            note = "<strong>NOTE: outside applicability domain</strong><br>\n"
        qualifier = "weak"
        if self.stachelhaus_match_count == 10:
            qualifier = "strong"
        elif self.stachelhaus_match_count > 7:
            qualifier = "moderate"

        raw = ("\n"
               "<dl><dt>SVM prediction details:</dt>\n"
               " <dd>"
               "  %s"
               "  <dl>"
               "   <dt>Predicted physicochemical class:</dt>\n"
               "   <dd>%s</dd>\n"
               "   <dt>Large clusters prediction:</dt>\n"
               "   <dd>%s</dd>\n"
               "   <dt>Small clusters prediction:</dt>\n"
               "   <dd>%s</dd>\n"
               "   <dt>Single AA prediction:</dt>\n"
               "   <dd>%s</dd>\n"
               "  </dl>\n"
               " </dd>\n"
               "</dl>\n"
               "<dl><dt>Stachelhaus prediction details:</dt>\n"
               " <dd>\n"
               "  <dl>\n"
               "   <dt>Stachelhaus sequence:</dt>\n"
               "   <dd><span class=\"serif\">%s</span></dd>\n"
               "   <dt>Nearest Stachelhaus code:</dt>\n"
               "   <dd>%s</dd>\n"
               "   <dt>Stachelhaus code match:</dt>\n"
               "   <dd>%d%% (%s)</dd>\n"
               "  </dl>\n"
               " </dd>\n"
               "</dl>\n" % (note, self.physicochemical_class, ", ".join(self.large_cluster_pred),
                            ", ".join(self.small_cluster_pred), self.single_amino_pred,
                            self.stachelhaus_seq, ", ".join(self.stachelhaus_predictions),
                            self.stachelhaus_match_count * 10, qualifier))
        return Markup(raw)

    @classmethod
    def from_line(cls, line: str) -> "PredictorSVMResult":
        """ Generates a PredictorSVMResult from a line of NRPSPredictor2 output """
        parts = line.split("\t")
        # 0: sequence-id
        # 1: 8A-signature
        # 2: stachelhaus-code:
        # 3: 3class-pred
        # 4: large-class-pred
        # 5: small-class-pred
        # 6: single-class-pred
        # 7: nearest stachelhaus code
        # 8: NRPS1pred-large-class-pred
        # 9: NRPS2pred-large-class-pred
        # 10: outside applicability domain (1 or 0)
        # 11: coords
        # 12: pfam-score
        if not len(parts) == 13:
            raise ValueError("Invalid SVM result line: %s" % line)
        query_stach = parts[2]
        pred_from_stach = parts[7]
        best_stach_match = query_stach.lower()
        stach_count = 0
        for possible_hit in KNOWN_STACH_CODES[pred_from_stach]:
            # the datafile sometimes has - for the trailing char, but not all the time
            matches = [int(a == b) for a, b in list(zip(query_stach, possible_hit))[:9]] + [1]
            count = sum(matches)
            if count > stach_count:
                stach_count = count
                best_stach_match = "".join(c if match else c.lower() for (c, match) in zip(query_stach, matches))

        return cls(parts[1], parts[3], parts[4].split(","), parts[5].split(","),
                   parts[6], pred_from_stach.split("/"), parts[10] == "1", best_stach_match, stach_count)

    def __str__(self) -> str:
        return "PredictorSVMResult: " + str(vars(self))

    def to_json(self) -> Dict[str, Any]:
        return vars(self)

    @classmethod
    def from_json(cls, json: Dict[str, Any]) -> "PredictorSVMResult":
        return PredictorSVMResult(json["angstrom_code"], json["physicochemical_class"],
                                  json["large_cluster_pred"], json["small_cluster_pred"],
                                  json["single_amino_pred"], json["stachelhaus_predictions"],
                                  json["uncertain"], json["stachelhaus_seq"],
                                  json["stachelhaus_match_count"])



def read_output(lines: List[str]) -> Dict[str, Prediction]:
    """ Converts NRPSPredictor2 output lines to Predictions

        Arguments:
            lines: a list of result lines (without the header) from NRPSPredictor2

        Returns:
            a dictionary mapping each domain name to a PredictorSVMResult
    """
    results: Dict[str, Prediction] = {}
    for line in lines:
        results[line.split('\t')[0]] = PredictorSVMResult.from_line(line)
    return results


def run_nrpspredictor(a_domains: List[ModularDomain], options: ConfigType) -> Dict[str, Prediction]:
    """ Runs NRPSPredictor2 over the provided A domains.

        Arguments:
            a_domains: a list of ModularDomains, one for each A domain
            options: antismash options

        Returns:
            a dictionary mapping each domain name to a PredictorSVMResult
    """
    # NRPSPredictor: extract AMP-binding + 120 residues N-terminal of this domain,
    # extract 8 Angstrom residues and insert this into NRPSPredictor
    nrps_predictor_dir = path.get_full_path(__file__, "external", "NRPSPredictor2")
    data_dir = os.path.join(nrps_predictor_dir, 'data')
    lib_dir = os.path.join(nrps_predictor_dir, 'lib')
    jar_file = os.path.join(nrps_predictor_dir, 'build', 'NRPSpredictor2.jar')
    java_separator = ":"
    if sys.platform == "win32":
        java_separator = ";"
    classpath = java_separator.join([jar_file,
                                     os.path.join(lib_dir, 'java-getopt-1.0.13.jar'),
                                     os.path.join(lib_dir, 'Utilities.jar'),
                                     os.path.join(lib_dir, 'libsvm.jar')])
    input_filename = "signatures.fa"
    output_filename = "svm_output.txt"
    bacterial = "1" if options.taxon == "bacteria" else '0'

    signatures = [get_a_dom_signatures(a_domain)[1] for a_domain in a_domains]

    with TemporaryDirectory(change=True):
        # Get NRPSPredictor2 code predictions, output sig file for input for NRPSPredictor2 SVMs
        with open(input_filename, "w") as handle:
            for sig, domain in zip(signatures, a_domains):
                handle.write("%s\t%s\n" % (sig, domain.get_name()))
        # Run NRPSPredictor2 SVM
        commands = ['java',
                    '-Ddatadir=%s' % data_dir,
                    '-cp', classpath,
                    'org.roettig.NRPSpredictor2.NRPSpredictor2',
                    '-i', input_filename,
                    '-r', output_filename,
                    '-s', '1',
                    '-b', bacterial]
        result = subprocessing.execute(commands)
        if not result.successful():
            raise RuntimeError("NRPSPredictor2 failed: %s" % result.stderr)

        with open(output_filename) as handle:
            lines = handle.read().splitlines()[1:]  # strip the header

    return read_output(lines)


def map_nrpspredicor_to_norine(as_name: str) -> str:
    """ Maps NRPSPredictor amino acid nomenclature to NORINE """

    as_replacement_dict = {
        'bht': 'bOH-Tyr',
        'dhb': 'diOH-Bz',
        'iva': 'Ival',
        'pip': 'Hpr',
        'sal': 'diOH-Bz',
        'nrp': 'X',
        # TODO: different uses for the two seqs in NRPSPredictor
        # Q06YZ1_m4 is 3,5-dichloro-4-hydroxyphenylglycine
        # Q7WZ65_m1 is 3,5-dihydroxyphenylglycine
        'dpg': 'Cl2-Hpg',
        'ala-b': 'bAla',
        'b-ala': 'bAla',
        'beta-ala': 'bAla',
        'ala-d': 'D-Ala',
        'allo-thr': 'aThr',
        'hiv-d': 'D-Hiv',
        'alle': 'aIle',  # NRPSPredictor has a typo in there
        'alloile': 'aIle',
        'hmp-d': 'D-Hmp',
        '3-me-glu': '3Me-Glu',
        'lys-b': 'bLys',
    }
    return as_replacement_dict.get(as_name, as_name)
