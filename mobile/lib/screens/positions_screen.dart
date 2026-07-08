import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Open positions the engine is managing — plain English, read-only.
class PositionsScreen extends StatelessWidget {
  const PositionsScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/positions'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final map = data is Map ? data : const {};
        final raw = map['items'] is List ? map['items'] as List : const [];
        final items = raw
            .whereType<Map>()
            .where((r) => asText(firstOf(r, ['symbol'])) != '—')
            .toList();
        final monitorRunning = map['monitor_running'] == true;

        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
              child: Row(
                children: [
                  Text('${items.length} open',
                      style: Theme.of(context).textTheme.titleMedium),
                  const Spacer(),
                  StatusPill(
                    monitorRunning ? 'Monitor running' : 'Monitor down',
                    status: monitorRunning ? 'ok' : 'down',
                  ),
                ],
              ),
            ),
            if (items.isEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 80),
                child: Center(child: Text('No open positions.')),
              ),
            for (final p in items)
              InfoCard(
                title:
                    '${asText(firstOf(p, ['symbol']))}  ·  ${asText(firstOf(p, ['direction', 'side', 'dir'])).toUpperCase()}',
                trailing: StatusPill(humanize(firstOf(p, ['status', 'state']))),
                rows: [
                  MapEntry('Entry', Text(trimNum(firstOf(p, ['entry', 'entry_price'])))),
                  MapEntry('Current price', Text(trimNum(firstOf(p, ['mark', 'mark_price', 'last'])))),
                  MapEntry('Stop loss', Text(trimNum(firstOf(p, ['stop_loss', 'sl', 'stop'])))),
                  MapEntry('Profit / loss', _pnl(context, p)),
                  MapEntry('Held for', Text(minutesToDuration(asNum(firstOf(p, ['minutes_open', 'age_min']))))),
                ],
              ),
          ],
        );
      },
    );
  }

  Widget _pnl(BuildContext context, Map p) {
    final v = asNum(firstOf(p, ['upnl_pct', 'pnl_pct', 'unrealized_pct']));
    if (v == null) return Text(asText(firstOf(p, ['upnl', 'pnl'])));
    return Text(
      signedPct(v),
      style: TextStyle(
        fontWeight: FontWeight.w600,
        color: v >= 0 ? const Color(0xFF16A34A) : const Color(0xFFDC2626),
      ),
    );
  }
}
