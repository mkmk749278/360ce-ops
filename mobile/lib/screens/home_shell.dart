import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../app_keys.dart';
import '../auth/auth_service.dart';
import '../config.dart';
import '../push/push_service.dart';
import '../push/push_state.dart';
import 'activity_screen.dart';
import 'alerts_screen.dart';
import 'control_screen.dart';
import 'invalidations_screen.dart';
import 'pairs_screen.dart';
import 'positions_screen.dart';
import 'profit_screen.dart';
import 'pulse_screen.dart';
import 'signals_screen.dart';

/// Bottom-nav shell (core screens) + a drawer (secondary screens). Holds one
/// [auth]-derived ApiClient shared across screens; [onLoggedOut] fires on 401
/// or manual logout.
class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.auth, required this.onLoggedOut});
  final AuthService auth;
  final VoidCallback onLoggedOut;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  bool _lockEnabled = false;
  late final PushService _push = PushService(widget.auth, available: firebaseReady);

  static const _titles = ['Pulse', 'Signals', 'Positions', 'Profit', 'Control'];

  @override
  void initState() {
    super.initState();
    _push.start();
    widget.auth.biometricEnabled().then((v) {
      if (mounted) setState(() => _lockEnabled = v);
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => _applyRequestedTab());
    requestedTab.addListener(_applyRequestedTab);
  }

  @override
  void dispose() {
    requestedTab.removeListener(_applyRequestedTab);
    super.dispose();
  }

  void _applyRequestedTab() {
    final target = requestedTab.value;
    if (target == null) return;
    requestedTab.value = null;
    if (!mounted) return;
    if (target >= 0 && target < _titles.length) setState(() => _index = target);
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.auth.client();
    final onUnauthorized = widget.onLoggedOut;

    final screens = [
      PulseScreen(api: api, onUnauthorized: onUnauthorized),
      SignalsScreen(api: api, onUnauthorized: onUnauthorized),
      PositionsScreen(api: api, onUnauthorized: onUnauthorized),
      ProfitScreen(api: api, onUnauthorized: onUnauthorized),
      ControlScreen(api: api, onUnauthorized: onUnauthorized),
    ];

    return Scaffold(
      appBar: AppBar(title: Text('${Config.appName} · ${_titles[_index]}')),
      drawer: _buildDrawer(context, api, onUnauthorized),
      body: IndexedStack(index: _index, children: screens),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.favorite_outline), selectedIcon: Icon(Icons.favorite), label: 'Pulse'),
          NavigationDestination(icon: Icon(Icons.podcasts_outlined), selectedIcon: Icon(Icons.podcasts), label: 'Signals'),
          NavigationDestination(icon: Icon(Icons.candlestick_chart_outlined), selectedIcon: Icon(Icons.candlestick_chart), label: 'Positions'),
          NavigationDestination(icon: Icon(Icons.trending_up_outlined), selectedIcon: Icon(Icons.trending_up), label: 'Profit'),
          NavigationDestination(icon: Icon(Icons.tune_outlined), selectedIcon: Icon(Icons.tune), label: 'Control'),
        ],
      ),
    );
  }

  Widget _buildDrawer(BuildContext context, ApiClient api, VoidCallback onUnauthorized) {
    void open(String title, Widget screen) {
      Navigator.pop(context);
      Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => Scaffold(
          appBar: AppBar(title: Text('${Config.appName} · $title')),
          body: screen,
        ),
      ));
    }

    return Drawer(
      child: SafeArea(
        child: ListView(
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(16, 16, 16, 8),
              child: Text('More', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 18)),
            ),
            ListTile(
              leading: const Icon(Icons.history),
              title: const Text('Activity'),
              onTap: () => open('Activity', ActivityScreen(api: api, onUnauthorized: onUnauthorized)),
            ),
            ListTile(
              leading: const Icon(Icons.notifications_active_outlined),
              title: const Text('Alerts'),
              onTap: () => open('Alerts', AlertsScreen(api: api, onUnauthorized: onUnauthorized)),
            ),
            ListTile(
              leading: const Icon(Icons.list_alt),
              title: const Text('Pairs'),
              onTap: () => open('Pairs', PairsScreen(api: api, onUnauthorized: onUnauthorized)),
            ),
            ListTile(
              leading: const Icon(Icons.rule),
              title: const Text('Invalidations'),
              onTap: () => open('Invalidations', InvalidationsScreen(api: api, onUnauthorized: onUnauthorized)),
            ),
            const Divider(),
            SwitchListTile(
              secondary: const Icon(Icons.fingerprint),
              title: const Text('App lock'),
              subtitle: const Text('Require biometric to open'),
              value: _lockEnabled,
              onChanged: _toggleLock,
            ),
            ListTile(
              leading: const Icon(Icons.logout),
              title: const Text('Log out'),
              onTap: () async {
                Navigator.pop(context);
                await _push.unregister();
                await widget.auth.logout();
                widget.onLoggedOut();
              },
            ),
            ListTile(
              leading: const Icon(Icons.phonelink_erase),
              title: const Text('Revoke all devices'),
              onTap: () => _revokeAll(context),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _toggleLock(bool value) async {
    if (value) {
      if (!await widget.auth.biometricsAvailable()) {
        _snack('No biometric enrolled on this device.');
        return;
      }
      if (!await widget.auth.unlock()) {
        _snack('Could not verify biometric.');
        return;
      }
    }
    await widget.auth.setBiometricEnabled(value);
    if (mounted) setState(() => _lockEnabled = value);
    _snack(value ? 'App lock on.' : 'App lock off.');
  }

  void _snack(String msg) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  Future<void> _revokeAll(BuildContext context) async {
    Navigator.pop(context);
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
