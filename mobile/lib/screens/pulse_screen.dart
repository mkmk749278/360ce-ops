import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Engine health at a glance — plain English, no raw data.
class PulseScreen extends StatelessWidget {
  const PulseScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/pulse'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final map = data is Map ? data : const {};
        final p = map['pulse'] is Map ? map['pulse'] as Map : const {};

        final status = asText(firstOf(p, ['status', 'health', 'state']));
        final mode = asText(firstOf(p, ['mode']));
        final pnlUsd = firstOf(p, ['today_pnl_usd']);
        final pnlPct = firstOf(p, ['today_pnl_pct']);
        final lossUsed = firstOf(p, ['daily_loss_used_usd']);
        final lossBudget = firstOf(p, ['daily_loss_budget_usd']);

        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            InfoCard(
              title: 'Engine',
              trailing: StatusPill(humanize(status), status: status),
              rows: [
                MapEntry('Trading mode', StatusPill(humanize(mode), status: mode)),
                MapEntry('Market regime', Text(humanize(firstOf(p, ['regime'])))),
                MapEntry('Open positions', Text(asText(firstOf(p, ['open_positions'])))),
                MapEntry('Watching', Text('${asText(firstOf(p, ['scanning_pairs']))} pairs')),
                MapEntry('Running for', Text(secondsToDuration(firstOf(p, ['uptime_seconds'])))),
              ],
            ),
            InfoCard(
              title: 'Today',
              rows: [
                MapEntry(
                  'Profit / loss',
                  Text(
                    pnlUsd == null
                        ? '—'
                        : '${money(pnlUsd)}  (${signedPct(asNum(pnlPct))})',
                    style: TextStyle(
                      fontWeight: FontWeight.w600,
                      color: _pnlColor(context, asNum(pnlUsd)),
                    ),
                  ),
                ),
                MapEntry(
                  'Loss budget used',
                  Text(lossBudget == null
                      ? '—'
                      : '${money(lossUsed)} of ${money(lossBudget)}'),
                ),
                MapEntry('Signals today', Text(asText(firstOf(p, ['signals_today'])))),
              ],
            ),
            InfoCard(
              title: 'Scanner heartbeat',
              rows: [
                MapEntry('Status', Text(_heartbeat(map['heartbeat']))),
              ],
            ),
          ],
        );
      },
    );
  }

  Color? _pnlColor(BuildContext context, num? v) {
    if (v == null) return null;
    return v >= 0 ? const Color(0xFF16A34A) : const Color(0xFFDC2626);
  }

  String _heartbeat(dynamic hb) {
    if (hb is Map) {
      final age = firstOf(hb, ['age_sec', 'age']);
      if (age != null) return 'Last scan ${secondsToDuration(age)} ago';
      return asText(firstOf(hb, ['state', 'status']));
    }
    return asText(hb);
  }
}
