// Firebase options for the ops app.
//
// The Android block below ships with PLACEHOLDER values. CI substitutes the
// real values from the owner-provided google-services.json at build time (see
// .github/workflows/mobile-apk.yml). When the secret isn't set the placeholders
// remain and Firebase init fails gracefully at runtime — the app runs with
// push disabled (see main.dart).
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/foundation.dart' show defaultTargetPlatform, TargetPlatform;

class DefaultFirebaseOptions {
  static FirebaseOptions get currentPlatform {
    switch (defaultTargetPlatform) {
      case TargetPlatform.android:
        return android;
      default:
        return android;
    }
  }

  static const FirebaseOptions android = FirebaseOptions(
    apiKey: 'PLACEHOLDER_API_KEY',
    appId: 'PLACEHOLDER_APP_ID',
    messagingSenderId: 'PLACEHOLDER_SENDER_ID',
    projectId: 'PLACEHOLDER_PROJECT_ID',
    storageBucket: 'PLACEHOLDER_STORAGE_BUCKET',
  );
}
