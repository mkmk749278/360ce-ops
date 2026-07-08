import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';

import 'app.dart';
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
    firebaseReady = true;
  } catch (_) {
    firebaseReady = false;
  }
  runApp(const OpsApp());
}
