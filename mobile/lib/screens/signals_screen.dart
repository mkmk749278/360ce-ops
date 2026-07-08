import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Active signals / trade setups the engine is tracking.
class SignalsScreen extends StatelessWidget {
  const SignalsScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/signals'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final items = _extractList(data);
        if (items.isEmpty) {
          return ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [
              SizedBox(height: 120),
              Center(child: Text('No active signals.')),
            ],
          );
        }
        return ListView.builder(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: items.length,
          itemBuilder: (context, i) {
            final s = items[i];
            if (s is! Map) return JsonCard(s, title: 'Signal');
            final symbol = asText(firstOf(s, ['symbol', 'pair']));
            final dir = asText(firstOf(s, ['direction', 'dir', 'side'])).toUpperCase();
            final setup = asText(firstOf(s, ['setup', 'setup_class', 'channel']));
            final conf = firstOf(s, ['confidence', 'conf', 'score']);
            final status = asText(firstOf(s, ['status', 'state']));
            return InfoCard(
              title: '$symbol  ·  $dir',
              trailing: StatusPill(status.isEmpty ? setup : status, status: status),
              rows: [
                MapEntry('Setup', Text(setup)),
                MapEntry('Confidence', Text(conf == null ? '—' : fixed(conf as num, decimals: 1))),
                MapEntry('Entry', Text(asText(firstOf(s, ['entry', 'entry_price', 'price'])))),
                MapEntry('Stop', Text(asText(firstOf(s, ['stop_loss', 'sl', 'stop'])))),
                MapEntry('Target', Text(asText(firstOf(s, ['take_profit', 'tp', 'target'])))),
                MapEntry('ID', Text(shortId(asText(firstOf(s, ['signal_id', 'id']))))),
              ],
            );
          },
        );
      },
    );
  }

  List<dynamic> _extractList(dynamic data) {
    if (data is List) return data;
    if (data is Map) {
      for (final k in ['signals', 'items', 'active', 'results']) {
        if (data[k] is List) return data[k] as List;
      }
    }
    return const [];
  }
}
