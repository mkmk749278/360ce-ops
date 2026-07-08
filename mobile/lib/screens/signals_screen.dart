import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Active signals / trade setups the engine is tracking — plain English.
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
            children: const [SizedBox(height: 120), Center(child: Text('No active signals right now.'))],
          );
        }
        return ListView.builder(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: items.length,
          itemBuilder: (context, i) {
            final s = items[i] is Map ? items[i] as Map : const {};
            final symbol = asText(firstOf(s, ['symbol', 'pair']));
            final dir = asText(firstOf(s, ['direction', 'dir', 'side'])).toUpperCase();
            final status = asText(firstOf(s, ['status', 'state']));
            final conf = asNum(firstOf(s, ['confidence', 'conf', 'score']));
            return InfoCard(
              title: '$symbol  ·  $dir',
              trailing: StatusPill(humanize(status), status: status),
              rows: [
                MapEntry('Strategy', Text(humanize(firstOf(s, ['setup', 'setup_class', 'channel'])))),
                MapEntry('Confidence', Text(conf == null ? '—' : '${conf.toStringAsFixed(0)} / 100')),
                MapEntry('Entry', Text(trimNum(firstOf(s, ['entry', 'entry_price', 'price'])))),
                MapEntry('Stop loss', Text(trimNum(firstOf(s, ['stop_loss', 'sl', 'stop'])))),
                MapEntry('Target', Text(trimNum(firstOf(s, ['take_profit', 'tp1', 'tp', 'target'])))),
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
