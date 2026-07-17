/// Pins the engine-code → English rendering used across every screen.
///
/// The engine speaks UPPER_SNAKE enums; a regression here garbles labels on
/// the control surface the owner reads during incidents (e.g. kill-switch /
/// position states), so the acronym-preservation rules get pinned.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:ops360/util/humanize.dart';

void main() {
  group('humanize', () {
    test('null and empty collapse to the em-dash placeholder', () {
      expect(humanize(null), '—');
      expect(humanize(''), '—');
      expect(humanize('   '), '—');
      expect(humanize('—'), '—');
    });

    test('upper-snake enums become title case', () {
      expect(humanize('FAILED_AUCTION_RECLAIM'), 'Failed Auction Reclaim');
      expect(humanize('QUIET'), 'Quiet');
    });

    test('known trading acronyms stay upper-case', () {
      expect(humanize('MOVER_AVWAP_SCALP'), 'Mover AVWAP Scalp');
      expect(humanize('TP1_HIT'), 'TP1 Hit');
      expect(humanize('SL_HIT'), 'SL Hit');
      expect(humanize('MTF_GATE'), 'MTF Gate');
    });

    test('lower-case values are capitalised', () {
      expect(humanize('paper'), 'Paper');
      expect(humanize('live'), 'Live');
    });

    test('mixed separators and repeated whitespace are normalised', () {
      expect(humanize('be_shift  done'), 'BE Shift Done');
    });

    test('non-string values render via toString', () {
      expect(humanize(42), '42');
    });
  });
}
