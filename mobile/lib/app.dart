import 'package:flutter/material.dart';

import 'app_keys.dart';
import 'auth/auth_service.dart';
import 'config.dart';
import 'screens/home_shell.dart';
import 'screens/login_screen.dart';
import 'screens/lock_screen.dart';
import 'theme.dart';

enum _Stage { loading, login, locked, home }

/// Root widget. Drives the auth/unlock state machine:
/// loading → (token? locked : login) → home.
class OpsApp extends StatefulWidget {
  const OpsApp({super.key, AuthService? auth}) : _injectedAuth = auth;
  final AuthService? _injectedAuth;

  @override
  State<OpsApp> createState() => _OpsAppState();
}

class _OpsAppState extends State<OpsApp> {
  late final AuthService _auth = widget._injectedAuth ?? AuthService();
  _Stage _stage = _Stage.loading;

  @override
  void initState() {
    super.initState();
    _bootstrap();
  }

  Future<void> _bootstrap() async {
    final hasToken = await _auth.load();
    if (!mounted) return;
    setState(() => _stage = hasToken ? _Stage.locked : _Stage.login);
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: Config.appName,
      navigatorKey: navigatorKey,
      scaffoldMessengerKey: scaffoldMessengerKey,
      debugShowCheckedModeBanner: false,
      theme: OpsTheme.light(),
      darkTheme: OpsTheme.dark(),
      themeMode: ThemeMode.system,
      home: _buildStage(),
    );
  }

  Widget _buildStage() {
    switch (_stage) {
      case _Stage.loading:
        return const Scaffold(body: Center(child: CircularProgressIndicator()));
      case _Stage.login:
        return LoginScreen(
          auth: _auth,
          onLoggedIn: () => setState(() => _stage = _Stage.home),
        );
      case _Stage.locked:
        return LockScreen(
          auth: _auth,
          onUnlocked: () => setState(() => _stage = _Stage.home),
          onCancel: () async {
            await _auth.logout();
            if (mounted) setState(() => _stage = _Stage.login);
          },
        );
      case _Stage.home:
        return HomeShell(
          auth: _auth,
          onLoggedOut: () => setState(() => _stage = _Stage.login),
        );
    }
  }
}
