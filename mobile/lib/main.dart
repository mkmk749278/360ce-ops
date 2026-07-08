import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';

import 'app.dart';
import 'app_keys.dart';
import 'firebase_options.dart';
import 'push/push_state.dart';

/// Background message handler. FCM "notification" messages are shown by the OS
/// automatically when the app is backgrounded; this handler exists so data-only
/// messages don't get dropped. Must be a top-level, vm-entry-point function.
@pragma('vm:entry-point')
Future<void> _firebaseBackgroundHandler(RemoteMessage message) async {
  // No-op: alerts carry a notification block, so the system tray handles them.
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // Guarded: without google-services.json wired at build time the options are
  // placeholders and init throws — the app then runs with push disabled.
  try {
    await Firebase.initializeApp(options: DefaultFirebaseOptions.currentPlatform);
    FirebaseMessaging.onBackgroundMessage(_firebaseBackgroundHandler);
    _setupPushHandlers();
    firebaseReady = true;
  } catch (_) {
    firebaseReady = false;
  }
  runApp(const OpsApp());
}

/// Foreground display + deep-link routing for push. Android doesn't show a
/// system notification for a foreground message, so we surface a SnackBar; a
/// tapped notification (background or cold start) asks the shell to open the
/// Pulse tab (engine state) via [requestedTab].
void _setupPushHandlers() {
  FirebaseMessaging.onMessage.listen((message) {
    final n = message.notification;
    if (n == null) return;
    final text = [n.title, n.body].where((s) => s != null && s.isNotEmpty).join(' — ');
    scaffoldMessengerKey.currentState?.showSnackBar(
      SnackBar(content: Text(text.isEmpty ? 'New alert' : text)),
    );
  });
  FirebaseMessaging.onMessageOpenedApp.listen((_) => requestedTab.value = 0);
  FirebaseMessaging.instance.getInitialMessage().then((m) {
    if (m != null) requestedTab.value = 0;
  });
}
