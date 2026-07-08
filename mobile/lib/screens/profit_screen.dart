import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/cards.dart';

/// Profit / signal-analysis surface — the exit-strategy replay the owner uses
/// to analyse signals. Window-selectable; plain-English summary, per-signal
/// rows, and score-band / regime / path breakdowns. No raw data.
class ProfitScreen extends StatefulWidget {
  const ProfitScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  State<ProfitScreen> createState() => _ProfitScreenState();
}

class _ProfitScreenState extends State<ProfitScreen> {
  static const _windows = {
    'live': 'Live', '24h': '24 hours', '3d': '3 days',
    '7d': '7 days', '30d': '30 days', 'all': 'All time',
  };
  String _window = 'live';
  Map _data = const {};
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final d = await widget.api.get('/profit', query: {'window': _window});
      setState(() {
        _data = d is Map ? d : const {};
        _loading = false;
      });
    } on UnauthorizedException {
      widget.onUnauthorized();
    } catch (e) {
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            children: [
              for (final e in _windows.entries)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(e.value),
                    selected: _window == e.key,
                    onSelected: _loading
                        ? null
                        : (_) {
                            setState(() => _window = e.key);
                            _load();
                          },
                  ),
                ),
            ],
          ),
        ),
        Expanded(child: _body()),
      ],
    );
  }

  Widget _body() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) {
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [const SizedBox(height: 100), Center(child: Text(_error!))]),
      );
    }
    final summary = _data['summary'] is Map ? _data['summary'] as Map : const {};
    final breakdown = _data['breakdown'] is Map ? _data['breakdown'] as Map : const {};
    final rows = _data['rows'] is List ? _data['rows'] as List : const [];

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 16),
        children: [
          if (summary.isNotEmpty)
            InfoCard(
              title: '${_data['count'] ?? rows.length} signals',
              rows: [
                for (final e in summary.entries)
                  MapEntry(humanize(e.key), Text(_val(e.key.toString(), e.value))),
              ],
            ),
          _breakdownCard('By score band', breakdown['scoreband']),
          _breakdownCard('By market regime', breakdown['regime']),
          _breakdownCard('By setup path', breakdown['path']),
          if (rows.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
              child: Text('Signals', style: Theme.of(context).textTheme.titleMedium),
            ),
          for (final r in rows.whereType<Map>()) _signalRow(r),
        ],
      ),
    );
  }

  /// Format a value based on its key: percentages, money, or trimmed number.
  String _val(String key, dynamic v) {
    final k = key.toLowerCase();
    final n = asNum(v);
    if (n == null) return humanize(v);
    if (k.contains('pct') || k.contains('rate') || k.contains('percent')) {
      return signedPct(n);
    }
    if (k.contains('usd') || k.contains('pnl') && k.contains('usd')) return money(n);
    return trimNum(n, maxFrac: 2);
  }

  Widget _breakdownCard(String title, dynamic list) {
    if (list is! List || list.isEmpty) return const SizedBox.shrink();
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 4, 4, 8),
              child: Text(title, style: Theme.of(context).textTheme.titleMedium),
            ),
            for (final item in list.whereType<Map>()) _breakdownRow(item),
          ],
        ),
      ),
    );
  }

  Widget _breakdownRow(Map m) {
    // First string-ish field is the label; numeric fields become the stats.
    final label = asText(firstOf(m, ['band', 'regime', 'path', 'label', 'name', 'key']));
    final stats = <String>[];
    for (final e in m.entries) {
      final v = asNum(e.value);
      if (v == null) continue;
      final k = e.key.toString().toLowerCase();
      if (k.contains('pct') || k.contains('rate')) {
        stats.add('${humanize(e.key)} ${signedPct(v)}');
      } else if (k == 'n' || k.contains('count') || k.contains('trades')) {
        stats.add('${v.toInt()} trades');
      }
    }
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(humanize(label), style: const TextStyle(fontWeight: FontWeight.w600)),
          if (stats.isNotEmpty)
            Text(stats.join('  ·  '),
                style: TextStyle(fontSize: 12, color: Theme.of(context).colorScheme.outline)),
        ],
      ),
    );
  }

  Widget _signalRow(Map r) {
    final sym = asText(firstOf(r, ['symbol', 'pair']));
    final side = asText(firstOf(r, ['side', 'direction', 'dir'])).toUpperCase();
    final resNum = asNum(firstOf(r, ['result_pct', 'strategy_pct', 'real_pnl_pct']));
    final status = firstOf(r, ['status', 'real_status', 'tp_reach']);
    final scheme = Theme.of(context).colorScheme;
    return ListTile(
      dense: true,
      title: Text('$sym  ·  $side'),
      subtitle: Text(
        '${humanize(firstOf(r, ['setup_class', 'setup']))} · ${humanize(firstOf(r, ['regime']))}',
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
      ),
      trailing: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Text(
            resNum == null ? '—' : signedPct(resNum),
            style: TextStyle(
              fontWeight: FontWeight.w700,
              color: resNum == null
                  ? scheme.outline
                  : (resNum >= 0 ? const Color(0xFF16A34A) : const Color(0xFFDC2626)),
            ),
          ),
          Text(humanize(status), style: TextStyle(fontSize: 11, color: scheme.outline)),
        ],
      ),
    );
  }
}
