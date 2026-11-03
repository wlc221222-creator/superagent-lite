package io.superagent.lite;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

/** Server address + token. Stored only in the app's private prefs. */
public final class SettingsActivity extends Activity {

    private EditText baseUrl;
    private EditText token;
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        baseUrl = findViewById(R.id.base_url);
        token = findViewById(R.id.token);
        status = findViewById(R.id.status);

        SharedPreferences p = getSharedPreferences("settings", MODE_PRIVATE);
        baseUrl.setText(p.getString("base_url", ""));
        token.setText(p.getString("token", ""));

        Button test = findViewById(R.id.test);
        Button save = findViewById(R.id.save);
        test.setOnClickListener(v -> testConnection());
        save.setOnClickListener(v -> {
            saveValues();
            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show();
            finish();
        });
    }

    private void saveValues() {
        ApiClient.savePrefs(this, baseUrl.getText().toString(), token.getText().toString());
    }

    private void testConnection() {
        saveValues();
        status.setText("正在连接…");
        ApiClient.fromPrefs(this).health(new ApiClient.Callback<String>() {
            @Override
            public void onSuccess(String value) {
                status.setText(value);
            }

            @Override
            public void onError(String message, String detail) {
                status.setText(message + "\n（详情：" + detail + "）");
            }
        });
    }
}
