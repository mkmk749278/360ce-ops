# 360 CE Ops — native app (`mobile/`)

Owner-only native Android app for controlling and monitoring the 360 engine.
The reliable, in-region replacement for the Telegram control/alert channel.
Design + roadmap: [`../docs/OPS_MOBILE_APP_PLAN.md`](../docs/OPS_MOBILE_APP_PLAN.md).

## What ships here (Phase 2)

Fully-native Flutter screens against the ops `/api/v1` surface:

- **Login** — enter the ops server URL + password once; the app receives an
  ops-issued **app-token** (never the engine's owner-tier Bearer) and stores it
  in the device keystore.
- **Biometric unlock** on every cold start.
- **Read screens**: Pulse (engine health), Signals, Positions, Performance,
  Control (state snapshot — writes arrive in Phase 3).
- Pull-to-refresh, Material-3, dark-first with a light variant.

## Architecture notes

- `lib/api/api_client.dart` — Dio client over `<baseUrl>/api/v1`, Bearer auth,
  typed `UnauthorizedException` (→ bounce to login) and `ApiException`.
- `lib/auth/auth_service.dart` — secure-storage token + base URL, login
  exchange, biometric unlock, `revoke-all` (lost-phone switch).
- `lib/util/format.dart` — pure, plugin-free helpers (unit-tested).
- Screens tolerate engine payload shape drift: they pull fields defensively and
  fall back to a raw-JSON card rather than crashing (mirrors the web
  dashboard's `tojson` convention).

## Build

CI (`.github/workflows/mobile-apk.yml`) builds the signed APK on every push
touching `mobile/**` and uploads it as a run artifact. It generates the Android
scaffolding with `flutter create` (deterministic), so `android/` is not
committed. The build is **debug-signed with no secrets**; set
`ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
`ANDROID_KEY_PASSWORD` as Actions secrets to switch to release signing.

Local:

```bash
cd mobile
flutter pub get
flutter test
flutter run                 # against ops.luminapp.org by default
flutter run --dart-define=OPS_BASE_URL=http://10.0.2.2:8000   # local ops
```
