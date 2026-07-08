import 'package:flutter/material.dart';

/// App-wide keys/state used by push handlers that live outside the widget tree.
///
/// - [scaffoldMessengerKey] lets a foreground FCM message raise a SnackBar
///   without a BuildContext.
/// - [requestedTab] carries a deep-link target: a tapped notification sets the
///   bottom-nav index it wants; [HomeShell] listens and switches to it.
final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();
final GlobalKey<ScaffoldMessengerState> scaffoldMessengerKey =
    GlobalKey<ScaffoldMessengerState>();
final ValueNotifier<int?> requestedTab = ValueNotifier<int?>(null);
