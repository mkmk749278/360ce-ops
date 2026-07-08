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
