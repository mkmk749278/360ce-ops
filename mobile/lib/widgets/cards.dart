import 'dart:convert';

import 'package:flutter/material.dart';

import '../theme.dart';

/// Small colored status pill (OK / WARN / DOWN etc.).
class StatusPill extends StatelessWidget {
  const StatusPill(this.label, {super.key, this.status});
  final String label;
  final String? status;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = OpsTheme.statusColor(scheme, status ?? label);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: color.withOpacity(0.16),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: TextStyle(color: color, fontWeight: FontWeight.w600, fontSize: 12),
      ),
    );
  }
}

/// A titled card wrapping arbitrary key/value rows.
class InfoCard extends StatelessWidget {
  const InfoCard({super.key, required this.title, required this.rows, this.trailing});
  final String title;
  final List<MapEntry<String, Widget>> rows;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(title, style: theme.textTheme.titleMedium),
                ),
                if (trailing != null) trailing!,
              ],
            ),
            const SizedBox(height: 8),
            for (final r in rows)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(
                      width: 130,
                      child: Text(
                        r.key,
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.outline,
                        ),
                      ),
                    ),
                    Expanded(child: DefaultTextStyle.merge(
                      style: theme.textTheme.bodyMedium,
                      child: r.value,
                    )),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Fallback that pretty-prints any JSON payload — mirrors the web dashboard's
/// "render unknown shapes as JSON rather than crash" convention, so a schema
/// change on the engine degrades to raw data instead of a blank screen.
class JsonCard extends StatelessWidget {
  const JsonCard(this.data, {super.key, this.title = 'Raw'});
  final dynamic data;
  final String title;

  @override
  Widget build(BuildContext context) {
    String pretty;
    try {
      pretty = const JsonEncoder.withIndent('  ').convert(data);
    } catch (_) {
      pretty = data.toString();
    }
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 8),
            SelectableText(
              pretty,
              style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }
}
