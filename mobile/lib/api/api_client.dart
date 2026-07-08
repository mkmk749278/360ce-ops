import 'package:dio/dio.dart';

import '../config.dart';

/// Thrown when the ops API rejects the app-token (401). The UI catches this to
/// bounce the owner back to the login screen.
class UnauthorizedException implements Exception {
  const UnauthorizedException();
}

/// Thrown for any other API failure, carrying a human-readable message the UI
/// can surface on an error card.
class ApiException implements Exception {
  const ApiException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Thin async client over the ops `/api/v1` surface. One instance is shared
/// app-wide (created after login, once the base URL + token are known).
class ApiClient {
  ApiClient({required String baseUrl, String? token})
      : _dio = Dio(
          BaseOptions(
            baseUrl: '$baseUrl/api/v1',
            connectTimeout: Config.requestTimeout,
            receiveTimeout: Config.requestTimeout,
            sendTimeout: Config.requestTimeout,
            // We inspect status codes ourselves so 401 becomes a typed error.
            validateStatus: (code) => code != null && code < 500,
            headers: token != null ? {'Authorization': 'Bearer $token'} : null,
          ),
        );

  final Dio _dio;

  /// Update the bearer token in place (e.g. after a fresh login).
  void setToken(String token) {
    _dio.options.headers['Authorization'] = 'Bearer $token';
  }

  Future<dynamic> get(String path, {Map<String, dynamic>? query}) =>
      _request(() => _dio.get(path, queryParameters: query));

  Future<dynamic> post(String path, {Object? body}) =>
      _request(() => _dio.post(path, data: body));

  Future<dynamic> delete(String path, {Object? body}) =>
      _request(() => _dio.delete(path, data: body));

  Future<dynamic> _request(Future<Response> Function() run) async {
    late final Response resp;
    try {
      resp = await run();
    } on DioException catch (e) {
      // Network-level failure (timeout, DNS, TLS, connection refused).
      throw ApiException(_friendly(e));
    }
    if (resp.statusCode == 401) {
      throw const UnauthorizedException();
    }
    if (resp.statusCode! >= 400) {
      final detail = resp.data is Map ? resp.data['detail'] : null;
      throw ApiException('HTTP ${resp.statusCode}${detail != null ? ' — $detail' : ''}');
    }
    return resp.data;
  }

  String _friendly(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.sendTimeout:
        return 'Timed out reaching ops — check the VPS / network.';
      case DioExceptionType.connectionError:
        return 'Cannot reach ops. Verify the server URL and that you are online.';
      case DioExceptionType.badCertificate:
        return 'TLS certificate error contacting ops.';
      default:
        return e.message ?? 'Network error contacting ops.';
    }
  }
}
