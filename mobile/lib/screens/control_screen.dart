import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Control state snapshot. Read-only in this build — the write actions
/// (auto-mode flip, kill switch, exec-mode, tunables) land in Phase 3 on the
/// owner-gated, audited ops control endpoints, with confirm on destructive
/// actions.
class ControlScreen extends StatelessWidget {
  const ControlScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/control/state'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final map = data is Map ? data : <String, dynamic>{};
        final auto = _m(map['auto_mode']);
        final ks = _m(map['kill_switch']);
        final glob = _m(map['auto_trade_global']);
        final expiry = _m(map['signal_expiry']);
        final tunables = _m(map['tunables']);

        final mode = asText(firstOf(auto, ['mode', 'value']));
        final ksEngaged = firstOf(ks, ['engaged', 'active', 'tripped']) == true;
        final globalOn = firstOf(glob, ['enabled', 'on']) == true;

        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            const _ReadOnlyBanner(),
            InfoCard(
              title: 'Execution',
              rows: [
                MapEntry('Auto-mode', StatusPill(mode, status: mode)),
                MapEntry('Kill switch',
                    StatusPill(ksEngaged ? 'ENGAGED' : 'clear', status: ksEngaged ? 'tripped' : 'ok')),
                MapEntry('Global auto-trade',
                    StatusPill(globalOn ? 'on' : 'off', status: globalOn ? 'ok' : 'off')),
                MapEntry('Signal expiry',
                    Text(asText(firstOf(expiry, ['enabled', 'value'])))),
              ],
            ),
            _TunablesCard(tunables: tunables),
          ],
        );
      },
    );
  }

  Map _m(dynamic v) => v is Map ? v : const {};
}

class _ReadOnlyBanner extends StatelessWidget {
  const _ReadOnlyBanner();
  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: scheme.secondaryContainer.withOpacity(0.5),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Icon(Icons.lock_outline, size: 18, color: scheme.onSecondaryContainer),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                'View-only. Control actions arrive in the next update.',
                style: TextStyle(color: scheme.onSecondaryContainer),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _TunablesCard extends StatelessWidget {
  const _TunablesCard({required this.tunables});
  final Map tunables;

  @override
  Widget build(BuildContext context) {
    final list = tunables['tunables'];
    if (list is! List || list.isEmpty) {
      return JsonCard(tunables, title: 'Tunables');
    }
    return InfoCard(
      title: 'Tunables',
      rows: [
        for (final t in list.whereType<Map>())
          MapEntry(
            asText(firstOf(t, ['label', 'key', 'name'])),
            Text(asText(firstOf(t, ['value', 'current']))),
          ),
      ],
    );
  }
}
