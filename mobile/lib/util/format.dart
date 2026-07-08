/// Pure formatting helpers used across screens. Kept free of Flutter/plugin
/// imports so they are unit-testable under `flutter test` without platform
/// channels.

/// Format a number as a signed percentage string, e.g. 1.8 -> "+1.80%".
String signedPct(num? value, {int decimals = 2}) {
  if (value == null) return '—';
  final sign = value > 0 ? '+' : '';
  return '$sign${value.toStringAsFixed(decimals)}%';
}

/// Compact money/number formatting with a fixed number of decimals, tolerant
/// of null.
String fixed(num? value, {int decimals = 2}) {
  if (value == null) return '—';
  return value.toStringAsFixed(decimals);
}

/// Coerce anything the engine hands back into a display string.
String asText(dynamic value) {
  if (value == null) return '—';
  if (value is String) return value.isEmpty ? '—' : value;
  return value.toString();
}

/// Shorten a long signal/order id for a chip label: keep head + tail.
String shortId(String? id, {int head = 6, int tail = 4}) {
  if (id == null || id.isEmpty) return '—';
  if (id.length <= head + tail + 1) return id;
  return '${id.substring(0, head)}…${id.substring(id.length - tail)}';
}

/// Humanize a minutes-open integer into "1h 05m" / "12m".
String minutesToDuration(num? minutes) {
  if (minutes == null) return '—';
  final total = minutes.round();
  if (total < 60) return '${total}m';
  final h = total ~/ 60;
  final m = total % 60;
  return '${h}h ${m.toString().padLeft(2, '0')}m';
}

/// Extract the first present, non-null value among [keys] from a map, so a
/// screen can tolerate the engine renaming a field without crashing.
dynamic firstOf(Map data, List<String> keys) {
  for (final k in keys) {
    final v = data[k];
    if (v != null) return v;
  }
  return null;
}

/// Coerce to num, or null.
num? asNum(dynamic v) => v is num ? v : (v is String ? num.tryParse(v) : null);

/// Trim float noise for display: whole numbers show plain, fractions round to
/// [maxFrac] and drop trailing zeros. 0.0040250285000000005 → "0.004025".
String trimNum(dynamic v, {int maxFrac = 6}) {
  final n = asNum(v);
  if (n == null) return asText(v);
  if (n == n.roundToDouble() && n.abs() < 1e15) return n.toInt().toString();
  var s = n.toStringAsFixed(maxFrac);
  if (s.contains('.')) {
    s = s.replaceFirst(RegExp(r'0+$'), '').replaceFirst(RegExp(r'\.$'), '');
  }
  return s;
}

/// Money with a fixed 2 decimals and sign, e.g. -12.5 → "-$12.50".
String money(dynamic v, {String symbol = '\$'}) {
  final n = asNum(v);
  if (n == null) return asText(v);
  return '${n < 0 ? '-' : ''}$symbol${n.abs().toStringAsFixed(2)}';
}

/// Seconds → "21h 40m" / "12m" / "45s".
String secondsToDuration(dynamic v) {
  final n = asNum(v);
  if (n == null) return '—';
  final total = n.round();
  if (total < 60) return '${total}s';
  return minutesToDuration(total ~/ 60);
}
