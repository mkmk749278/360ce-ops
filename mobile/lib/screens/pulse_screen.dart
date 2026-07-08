import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Engine health at a glance: running state, auto-mode, scanner heartbeat.
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
        final map = data is Map ? data : <String, dynamic>{};
        final pulse = map['pulse'] is Map ? map['pulse'] as Map : {};
        final auto = map['auto_mode'] is Map ? map['auto_mode'] as Map : {};
        final heartbeat = map['heartbeat'];

        final status = asText(firstOf(pulse, ['status', 'state', 'health'])).toLowerCase();
        final mode = asText(firstOf(auto, ['mode', 'auto_mode', 'value']));

        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            InfoCard(
              title: 'Engine',
              trailing: StatusPill(status.isEmpty ? 'unknown' : status, status: status),
              rows: [
                MapEntry('Auto-mode', StatusPill(mode, status: mode)),
                MapEntry('Uptime', Text(asText(firstOf(pulse, ['uptime', 'uptime_human', 'started_at'])))),
                MapEntry('Open positions', Text(asText(firstOf(pulse, ['open_positions', 'positions', 'open'])))),
                MapEntry('Scan pairs', Text(asText(firstOf(pulse, ['pairs', 'scan_pairs', 'universe'])))),
              ],
            ),
            InfoCard(
              title: 'Scanner heartbeat',
              rows: [
                MapEntry('Age', Text(_heartbeatAge(heartbeat))),
                MapEntry('State', Text(_heartbeatState(heartbeat))),
              ],
            ),
            JsonCard(map, title: 'Full pulse payload'),
          ],
        );
      },
    );
  }

  String _heartbeatAge(dynamic hb) {
    if (hb is Map) return asText(firstOf(hb, ['age_sec', 'age', 'heartbeat_age']));
    return asText(hb);
  }

  String _heartbeatState(dynamic hb) {
    if (hb is Map) return asText(firstOf(hb, ['state', 'status', 'fresh']));
    return '—';
  }
}
