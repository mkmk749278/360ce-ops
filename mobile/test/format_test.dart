import 'package:flutter_test/flutter_test.dart';
import 'package:ops360/util/format.dart';

void main() {
  group('signedPct', () {
    test('adds + for positive, keeps - for negative', () {
      expect(signedPct(1.8), '+1.80%');
      expect(signedPct(-1.0), '-1.00%');
      expect(signedPct(0), '0.00%');
    });
    test('null is em dash', () => expect(signedPct(null), '—'));
  });

  group('asText', () {
    test('null and empty become em dash', () {
      expect(asText(null), '—');
      expect(asText(''), '—');
    });
    test('passes through strings and stringifies others', () {
      expect(asText('BTCUSDT'), 'BTCUSDT');
      expect(asText(42), '42');
    });
  });

  group('shortId', () {
    test('shortens long ids, keeps short ones', () {
      expect(shortId('abcdef1234567890'), 'abcdef…7890');
      expect(shortId('short'), 'short');
      expect(shortId(null), '—');
    });
  });

  group('minutesToDuration', () {
    test('formats under and over an hour', () {
      expect(minutesToDuration(12), '12m');
      expect(minutesToDuration(65), '1h 05m');
      expect(minutesToDuration(null), '—');
    });
  });

  group('firstOf', () {
    test('returns first present key, else null', () {
      final m = {'a': null, 'b': 2, 'c': 3};
      expect(firstOf(m, ['a', 'b', 'c']), 2);
      expect(firstOf(m, ['x', 'y']), isNull);
    });
  });
}
