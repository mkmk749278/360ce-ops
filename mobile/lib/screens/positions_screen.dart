import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Open positions the engine is managing (read-only; per-position close is an
/// owner-sign-off action that lands with the control write surface).
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
        final map = data is Map ? data : <String, dynamic>{};
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
                    monitorRunning ? 'monitor up' : 'monitor down',
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
                trailing: StatusPill(asText(firstOf(p, ['status', 'state']))),
                rows: [
                  MapEntry('Entry', Text(asText(firstOf(p, ['entry', 'entry_price'])))),
                  MapEntry('Mark', Text(asText(firstOf(p, ['mark', 'mark_price', 'last'])))),
                  MapEntry('Stop', Text(asText(firstOf(p, ['stop_loss', 'sl', 'stop'])))),
                  MapEntry('uPnL', Text(_pnl(p))),
                  MapEntry('Open', Text(minutesToDuration(_num(firstOf(p, ['minutes_open', 'age_min']))))),
                ],
              ),
          ],
        );
      },
    );
  }

  String _pnl(Map p) {
    final v = _num(firstOf(p, ['upnl_pct', 'pnl_pct', 'unrealized_pct']));
    return v == null ? asText(firstOf(p, ['upnl', 'pnl'])) : signedPct(v);
  }

  num? _num(dynamic v) => v is num ? v : (v is String ? num.tryParse(v) : null);
}
