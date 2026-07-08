import 'package:flutter/material.dart';

import '../api/api_client.dart';

/// A pull-to-refresh scaffold body that runs an async [loader] and renders
/// loading / error / content states consistently. On [UnauthorizedException]
/// it calls [onUnauthorized] so the app can bounce to login.
class AsyncView extends StatefulWidget {
  const AsyncView({
    super.key,
    required this.loader,
    required this.builder,
    required this.onUnauthorized,
  });

  final Future<dynamic> Function() loader;
  final Widget Function(BuildContext context, dynamic data) builder;
  final VoidCallback onUnauthorized;

  @override
  State<AsyncView> createState() => _AsyncViewState();
}

class _AsyncViewState extends State<AsyncView> {
  late Future<dynamic> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.loader();
  }

  Future<void> _refresh() async {
    setState(() => _future = widget.loader());
    await _future.catchError((_) => null);
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<dynamic>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const _Centered(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            final err = snap.error;
            if (err is UnauthorizedException) {
              WidgetsBinding.instance
                  .addPostFrameCallback((_) => widget.onUnauthorized());
              return const _Centered(child: Text('Session expired…'));
            }
            return _ErrorState(message: err.toString(), onRetry: _refresh);
          }
          return widget.builder(context, snap.data);
        },
      ),
    );
  }
}

class _Centered extends StatelessWidget {
  const _Centered({required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) {
    // Wrapped in a scroll view so RefreshIndicator still works in empty states.
    return LayoutBuilder(
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: SizedBox(
          height: constraints.maxHeight,
          child: Center(child: child),
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.message, required this.onRetry});
  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return _Centered(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cloud_off_rounded, size: 48, color: scheme.error),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton.tonal(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}
