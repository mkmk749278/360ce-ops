import 'package:flutter/material.dart';

import '../auth/auth_service.dart';
import '../config.dart';
import '../push/push_service.dart';
import '../push/push_state.dart';
import 'control_screen.dart';
import 'performance_screen.dart';
import 'positions_screen.dart';
import 'pulse_screen.dart';
import 'signals_screen.dart';

/// Bottom-nav shell hosting the read screens. Holds one [auth]-derived
/// ApiClient shared across tabs; [onLoggedOut] fires on 401 or manual logout.
class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.auth, required this.onLoggedOut});
  final AuthService auth;
  final VoidCallback onLoggedOut;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  late final PushService _push = PushService(widget.auth, available: firebaseReady);

  static const _titles = ['Pulse', 'Signals', 'Positions', 'Perf', 'Control'];

  @override
  void initState() {
    super.initState();
    // Register this device for push alerts (no-op if Firebase isn't wired).
    _push.start();
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.auth.client();
    final onUnauthorized = widget.onLoggedOut;

    final screens = [
      PulseScreen(api: api, onUnauthorized: onUnauthorized),
      SignalsScreen(api: api, onUnauthorized: onUnauthorized),
      PositionsScreen(api: api, onUnauthorized: onUnauthorized),
      PerformanceScreen(api: api, onUnauthorized: onUnauthorized),
      ControlScreen(api: api, onUnauthorized: onUnauthorized),
    ];

    return Scaffold(
      appBar: AppBar(
        title: Text('${Config.appName} · ${_titles[_index]}'),
        actions: [
          PopupMenuButton<String>(
            onSelected: (v) => _onMenu(context, v),
            itemBuilder: (context) => const [
              PopupMenuItem(value: 'logout', child: Text('Log out')),
              PopupMenuItem(value: 'revoke', child: Text('Revoke all devices')),
            ],
          ),
        ],
      ),
      body: IndexedStack(index: _index, children: screens),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.favorite_outline), selectedIcon: Icon(Icons.favorite), label: 'Pulse'),
          NavigationDestination(icon: Icon(Icons.podcasts_outlined), selectedIcon: Icon(Icons.podcasts), label: 'Signals'),
          NavigationDestination(icon: Icon(Icons.candlestick_chart_outlined), selectedIcon: Icon(Icons.candlestick_chart), label: 'Positions'),
          NavigationDestination(icon: Icon(Icons.insights_outlined), selectedIcon: Icon(Icons.insights), label: 'Perf'),
          NavigationDestination(icon: Icon(Icons.tune_outlined), selectedIcon: Icon(Icons.tune), label: 'Control'),
        ],
      ),
    );
  }

  Future<void> _onMenu(BuildContext context, String value) async {
    if (value == 'logout') {
      await _push.unregister();
      await widget.auth.logout();
      widget.onLoggedOut();
      return;
    }
    if (value == 'revoke') {
      final ok = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Revoke all devices?'),
          content: const Text(
              'Every app-token is invalidated. Every device must log in again. Use this if a phone is lost.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Revoke')),
          ],
        ),
      );
      if (ok == true) {
        await _push.unregister();
        await widget.auth.revokeAllDevices();
        widget.onLoggedOut();
      }
    }
  }
}
