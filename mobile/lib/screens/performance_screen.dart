import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Recent performance / activity stats (mirrors the web performance page).
class PerformanceScreen extends StatelessWidget {
  const PerformanceScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/activity'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final map = data is Map ? data : <String, dynamic>{};
        final summary = map['summary'] is Map ? map['summary'] as Map : map;

        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            InfoCard(
              title: 'Performance',
              rows: [
                MapEntry('Trades', Text(asText(firstOf(summary, ['trades', 'count', 'n'])))),
                MapEntry('Win rate', Text(_pct(firstOf(summary, ['win_rate', 'wr', 'winrate'])))),
                MapEntry('Avg / trade', Text(_pct(firstOf(summary, ['avg_pct', 'expectancy', 'avg'])))),
                MapEntry('Total', Text(_pct(firstOf(summary, ['total_pct', 'total', 'sum_pct'])))),
                MapEntry('TP hits', Text(asText(firstOf(summary, ['tp_hits', 'tp'])))),
                MapEntry('SL hits', Text(asText(firstOf(summary, ['sl_hits', 'sl'])))),
              ],
            ),
            JsonCard(map, title: 'Full activity payload'),
          ],
        );
      },
    );
  }

  String _pct(dynamic v) {
    if (v is num) return signedPct(v);
    final parsed = v is String ? num.tryParse(v) : null;
    return parsed == null ? asText(v) : signedPct(parsed);
  }
}
