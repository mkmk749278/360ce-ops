import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../util/format.dart';
import '../util/humanize.dart';
import '../widgets/async_view.dart';

/// The pairs the engine is scanning / has promoted, grouped by category.
class PairsScreen extends StatelessWidget {
  const PairsScreen({super.key, required this.api, required this.onUnauthorized});
  final ApiClient api;
  final VoidCallback onUnauthorized;

  @override
  Widget build(BuildContext context) {
    return AsyncView(
      loader: () => api.get('/pairs'),
      onUnauthorized: onUnauthorized,
      builder: (context, data) {
        final sections = _sections(data);
        if (sections.isEmpty) {
          return ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const [SizedBox(height: 120), Center(child: Text('No pair data.'))],
          );
        }
        return ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            for (final s in sections)
              ExpansionTile(
                title: Text('${humanize(s.key)}  (${s.value.length})'),
                initiallyExpanded: sections.length <= 2,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                    child: Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        for (final p in s.value)
                          Chip(label: Text(_symbol(p))),
                      ],
                    ),
                  ),
                ],
              ),
          ],
        );
      },
    );
  }

  String _symbol(dynamic p) {
    if (p is String) return p;
    if (p is Map) return asText(firstOf(p, ['symbol', 'pair', 'name']));
    return asText(p);
  }

  List<MapEntry<String, List>> _sections(dynamic data) {
    final out = <MapEntry<String, List>>[];
    if (data is Map) {
      data.forEach((k, v) {
        if (v is List && v.isNotEmpty) out.add(MapEntry(k.toString(), v));
      });
    } else if (data is List && data.isNotEmpty) {
      out.add(MapEntry('Pairs', data));
    }
    return out;
  }
}
