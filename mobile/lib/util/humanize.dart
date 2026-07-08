/// Turn engine code strings into plain English for display.
///
/// The engine speaks in enums and snake/UPPER_CASE (`MOVER_AVWAP_SCALP`,
/// `TP1_HIT`, `FAILED_AUCTION_RECLAIM`, `QUIET`). The owner shouldn't have to
/// read code — these helpers render human labels while preserving known
/// trading acronyms (AVWAP, SR, TP1…).
library;

const Set<String> _acronyms = {
  'AVWAP', 'VWAP', 'SR', 'TP', 'TP1', 'TP2', 'TP3', 'SL', 'BE', 'MTF', 'FVG',
  'OI', 'RSI', 'ATR', 'EMA', 'MA', 'PNL', 'USD', 'USDT', 'ID', 'CE', 'HTF',
  'LTF', 'OB', 'BOS', 'CHOCH', 'ADX', 'AI', 'API', 'CVD',
};

/// "MOVER_AVWAP_SCALP" → "Mover AVWAP Scalp", "TP1_HIT" → "TP1 Hit",
/// "QUIET" → "Quiet", "paper" → "Paper".
String humanize(dynamic raw) {
  if (raw == null) return '—';
  final s = raw.toString().trim();
  if (s.isEmpty || s == '—') return '—';
  final words = s.replaceAll(RegExp(r'[_\s]+'), ' ').trim().split(' ');
  final out = <String>[];
  for (final w in words) {
    if (w.isEmpty) continue;
    final up = w.toUpperCase();
    if (_acronyms.contains(up)) {
      out.add(up);
    } else {
      out.add(w[0].toUpperCase() + w.substring(1).toLowerCase());
    }
  }
  return out.isEmpty ? '—' : out.join(' ');
}
