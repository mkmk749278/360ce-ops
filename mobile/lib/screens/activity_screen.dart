import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../widgets/async_view.dart';

/// Recent engine activity as a plain-English timeline (signals opening, hitting
/// targets, stopping out, invalidating).
class ActivityScreen extends StatelessWidget {
  const ActivityScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  static const _labels = {
    'OPEN': 'Opened',
    'TP1': 'Hit target 1',
    'TP2': 'Hit target 2',
    'TP3': 'Hit target 3',
    'SL': 'Stopped out',
    'INVAL': 'Invalidated',
    'CLOSE': 'Closed',
    'EXPIRE': 'Expired',
    'EXPIRED': 'Expired',
    'BE': 'Moved to break-even',
  };

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/activity'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final items = data is Map && data['items'] is List
            ? (data['items'] as List)
            : (data is List ? data : const []);
        if (items.isEmpty) {
          return ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [SizedBox(height: 120), Center(child: Text('No recent activity.'))],
          );
        }
        return ListView.separated(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: items.length,
          separatorBuilder: (_, __) => const Divider(height: 1, indent: 64),
          itemBuilder: (context, i) {
            final it = items[i] is Map ? items[i] as Map : const {};
            final kind = asText(it['kind']).toUpperCase();
            final label = _labels[kind] ?? kind;
            final head = asText(it['title']).split('—').first.trim();
            final sub = asText(it['subtitle']);
            final mins = asNum(it['minutes_ago']);
            final (icon, color) = _icon(context, kind);
            return ListTile(
              leading: CircleAvatar(
                backgroundColor: color.withOpacity(0.15),
                child: Icon(icon, color: color, size: 20),
              ),
              title: Text(head.isEmpty ? label : head),
              subtitle: Text(
                [label, if (sub != '—') sub].join(' · '),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
              trailing: Text(
                _ago(mins),
                style: TextStyle(fontSize: 12, color: Theme.of(context).colorScheme.outline),
              ),
            );
          },
        );
      },
    );
  }

  (IconData, Color) _icon(BuildContext context, String kind) {
    if (kind.startsWith('TP')) return (Icons.check_circle, const Color(0xFF16A34A));
    if (kind == 'SL' || kind == 'INVAL') return (Icons.cancel, const Color(0xFFDC2626));
    if (kind == 'OPEN') return (Icons.play_circle_fill, Theme.of(context).colorScheme.primary);
    if (kind == 'BE') return (Icons.shield, const Color(0xFFD97706));
    return (Icons.circle, Theme.of(context).colorScheme.outline);
  }

  String _ago(num? mins) {
    if (mins == null) return '';
    final m = mins.round();
    if (m <= 0) return 'now';
    if (m < 60) return '${m}m ago';
    final h = m ~/ 60;
    return h < 24 ? '${h}h ago' : '${h ~/ 24}d ago';
  }
}
