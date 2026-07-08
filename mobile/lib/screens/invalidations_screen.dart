import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';
import '../widgets/cards.dart';

/// Recent signal invalidations, classified protective / premature / neutral.
class InvalidationsScreen extends StatelessWidget {
  const InvalidationsScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/invalidations'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final records = _extract(data);
        if (records.isEmpty) {
          return ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [SizedBox(height: 120), Center(child: Text('No invalidations recorded.'))],
          );
        }
        return ListView.builder(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: records.length,
          itemBuilder: (context, i) {
            final r = records[i];
            final cls = asText(firstOf(r, ['classification', 'class', 'verdict']));
            return InfoCard(
              title:
                  '${asText(firstOf(r, ['symbol', 'pair']))}  ·  ${asText(firstOf(r, ['direction', 'side'])).toUpperCase()}',
              trailing: StatusPill(humanize(cls), status: _clsStatus(cls)),
              rows: [
                MapEntry('Why', Text(humanize(firstOf(r, ['reason', 'cause'])))),
                MapEntry('Setup', Text(humanize(firstOf(r, ['setup_class', 'setup'])))),
                MapEntry('Move after', Text(_pct(firstOf(r, ['move_after_pct', 'excursion_pct'])))),
              ],
            );
          },
        );
      },
    );
  }

  String _clsStatus(String cls) {
    switch (cls.toUpperCase()) {
      case 'PROTECTIVE':
        return 'ok';
      case 'PREMATURE':
        return 'error';
      default:
        return 'warn';
    }
  }

  String _pct(dynamic v) {
    final n = asNum(v);
    return n == null ? asText(v) : signedPct(n);
  }

  List<Map> _extract(dynamic data) {
    if (data is List) return data.whereType<Map>().toList();
    if (data is Map) {
      for (final k in ['records', 'items', 'invalidations']) {
        if (data[k] is List) return (data[k] as List).whereType<Map>().toList();
      }
    }
    return const [];
  }
}
