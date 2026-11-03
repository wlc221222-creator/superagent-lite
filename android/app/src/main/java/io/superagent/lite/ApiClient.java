package io.superagent.lite;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Minimal client for the superagent-lite HTTP API. All calls run on a
 * background executor; callbacks fire on the main thread. Error messages
 * are human sentences first; the technical detail rides along for the
 * "详情" expander (see DESIGN-BRIEF.md).
 */
public final class ApiClient {

    public interface Callback<T> {
        void onSuccess(T value);
        void onError(String message, String detail);
    }

    public static final class Ticket {
        public final String id;
        public final String description;
        public final String status;

        Ticket(String id, String description, String status) {
            this.id = id;
            this.description = description;
            this.status = status;
        }
    }

    private static final String PREFS = "settings";
    private final ExecutorService executor = Executors.newCachedThreadPool();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final String baseUrl;
    private final String token;

    private ApiClient(String baseUrl, String token) {
        this.baseUrl = baseUrl;
        this.token = token;
    }

    public static ApiClient fromPrefs(Context ctx) {
        SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return new ApiClient(
                p.getString("base_url", "").trim(),
                p.getString("token", "").trim());
    }

    public static void savePrefs(Context ctx, String baseUrl, String token) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("base_url", baseUrl.trim())
                .putString("token", token.trim())
                .apply();
    }

    public boolean isConfigured() {
        return !baseUrl.isEmpty();
    }

    // -- endpoints ------------------------------------------------------

    public void health(Callback<String> cb) {
        executor.execute(() -> {
            try {
                JSONObject json = request("GET", "/health", null, false, 15000);
                String auth = json.optString("auth", "?");
                post(() -> cb.onSuccess("连接正常（鉴权：" + ("token".equals(auth) ? "已开启" : "未开启") + "）"));
            } catch (ApiError e) {
                post(() -> cb.onError(e.getMessage(), e.detail));
            }
        });
    }

    public void chat(String text, Callback<String> cb) {
        executor.execute(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("text", text);
                body.put("user_id", "android");
                // /chat blocks while the server runs the task: allow 10 min.
                JSONObject json = request("POST", "/chat", body, true, 600000);
                String reply = json.optString("reply", "");
                post(() -> cb.onSuccess(reply.isEmpty() ? "（主智能体没有返回内容）" : reply));
            } catch (ApiError e) {
                post(() -> cb.onError(e.getMessage(), e.detail));
            } catch (Exception e) {
                post(() -> cb.onError("消息组织失败", String.valueOf(e)));
            }
        });
    }

    public void attention(Callback<List<Ticket>> cb) {
        executor.execute(() -> {
            try {
                JSONObject json = request("GET", "/attention", null, true, 30000);
                JSONArray arr = json.optJSONArray("tickets");
                List<Ticket> out = new ArrayList<>();
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject t = arr.optJSONObject(i);
                        if (t == null) continue;
                        out.add(new Ticket(
                                t.optString("id", ""),
                                t.optString("description", ""),
                                t.optString("status", "")));
                    }
                }
                post(() -> cb.onSuccess(out));
            } catch (ApiError e) {
                post(() -> cb.onError(e.getMessage(), e.detail));
            }
        });
    }

    public void approve(String ticketId, Callback<Void> cb) {
        act(ticketId, "approve", cb);
    }

    public void reject(String ticketId, Callback<Void> cb) {
        act(ticketId, "reject", cb);
    }

    private void act(String ticketId, String action, Callback<Void> cb) {
        executor.execute(() -> {
            try {
                request("POST", "/tickets/" + ticketId + "/" + action, null, true, 30000);
                post(() -> cb.onSuccess(null));
            } catch (ApiError e) {
                post(() -> cb.onError(e.getMessage(), e.detail));
            }
        });
    }

    // -- plumbing -------------------------------------------------------

    private static final class ApiError extends Exception {
        final String detail;
        ApiError(String message, String detail) {
            super(message);
            this.detail = detail;
        }
    }

    private JSONObject request(String method, String path, JSONObject body,
                               boolean auth, int readTimeoutMs) throws ApiError {
        if (baseUrl.isEmpty()) {
            throw new ApiError("还没有设置服务器地址", "base_url is empty");
        }
        HttpURLConnection conn = null;
        try {
            String url = baseUrl.replaceAll("/+$", "") + path;
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod(method);
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(readTimeoutMs);
            conn.setRequestProperty("Accept", "application/json");
            if (auth && !token.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + token);
            }
            if (body != null) {
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(bytes);
                }
            }
            int code = conn.getResponseCode();
            InputStream stream = code >= 200 && code < 300
                    ? conn.getInputStream() : conn.getErrorStream();
            String text = readAll(stream);
            JSONObject json = text.isEmpty() ? new JSONObject() : new JSONObject(text);
            if (code == 401) {
                throw new ApiError("访问令牌不正确，请在设置里检查", "HTTP 401: " + text);
            }
            if (code < 200 || code >= 300) {
                String serverError = json.optString("error", "");
                throw new ApiError(
                        serverError.isEmpty() ? "服务器返回了错误" : "服务器：" + serverError,
                        "HTTP " + code + ": " + text);
            }
            return json;
        } catch (ApiError e) {
            throw e;
        } catch (Exception e) {
            throw new ApiError("连接不上服务器，请检查地址和网络", String.valueOf(e));
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String readAll(InputStream stream) throws Exception {
        if (stream == null) return "";
        StringBuilder sb = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) sb.append(line);
        }
        return sb.toString();
    }

    private void post(Runnable r) {
        main.post(r);
    }
}
