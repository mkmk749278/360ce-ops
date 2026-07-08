import 'package:flutter/material.dart';

import '../auth/auth_service.dart';
import '../config.dart';

/// Biometric / device-PIN gate shown on cold start when a token is present.
/// Auto-prompts once; the owner can retry or cancel (which logs out).
class LockScreen extends StatefulWidget {
  const LockScreen({
    super.key,
    required this.auth,
    required this.onUnlocked,
    required this.onCancel,
  });
  final AuthService auth;
  final VoidCallback onUnlocked;
  final VoidCallback onCancel;

  @override
  State<LockScreen> createState() => _LockScreenState();
}

class _LockScreenState extends State<LockScreen> {
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _attempt());
  }

  Future<void> _attempt() async {
    if (_busy) return;
    setState(() => _busy = true);
    final ok = await widget.auth.unlock();
    if (!mounted) return;
    setState(() => _busy = false);
    if (ok) widget.onUnlocked();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.fingerprint, size: 72, color: scheme.primary),
            const SizedBox(height: 16),
            Text('Unlock ${Config.appName}',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _busy ? null : _attempt,
              child: const Text('Unlock'),
            ),
            TextButton(onPressed: widget.onCancel, child: const Text('Log out')),
          ],
        ),
      ),
    );
  }
}
