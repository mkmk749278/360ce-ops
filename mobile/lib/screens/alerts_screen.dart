import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Live safety alerts the monitoring agent has raised.
class AlertsScreen extends StatelessWidget {
  const AlertsScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/alerts'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final alerts = _extract(data);
        if (alerts.isEmpty) {
          return ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [SizedBox(height: 120), Center(child: Text('All clear — no active alerts.'))],
          );
        }
        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            for (final a in alerts)
              InfoCard(
                title: humanize(firstOf(a, ['fingerprint', 'name', 'id'])),
                trailing: StatusPill(
                  humanize(firstOf(a, ['severity', 'level'])),
                  status: asText(firstOf(a, ['severity', 'level'])) == 'HIGH' ? 'error' : 'warn',
                ),
                rows: [
                  MapEntry('Detail', Text(asText(firstOf(a, ['description', 'message', 'detail'])))),
                  MapEntry('Seen', Text('${asText(firstOf(a, ['count', 'cycles']))}× · first ${asText(firstOf(a, ['first_seen', 'since']))}')),
                ],
              ),
          ],
        );
      },
    );
  }

  List<Map> _extract(dynamic data) {
    if (data is List) return data.whereType<Map>().toList();
    if (data is Map) {
      for (final k in ['alerts', 'active', 'items']) {
        if (data[k] is List) return (data[k] as List).whereType<Map>().toList();
      }
      // Dict keyed by fingerprint → values are the alerts.
      final vals = data.values.whereType<Map>().toList();
      if (vals.isNotEmpty) return vals;
    }
    return const [];
  }
}
