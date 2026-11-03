package io.superagent.lite;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;

/** Chat screen: talk to the main agent on the server. */
public final class MainActivity extends Activity {

    private static final class Message {
        final boolean user;
        String text;
        Message(boolean user, String text) {
            this.user = user;
            this.text = text;
        }
    }

    private final List<Message> messages = new ArrayList<>();
    private MessageAdapter adapter;
    private EditText input;
    private Button sendButton;
    private ApiClient api;
    private boolean sending = false;
    private boolean settingsPrompted = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        ListView list = findViewById(R.id.messages);
        adapter = new MessageAdapter();
        list.setAdapter(adapter);
        input = findViewById(R.id.input);
        sendButton = findViewById(R.id.send);
        sendButton.setOnClickListener(v -> send());
    }

    @Override
    protected void onResume() {
        super.onResume();
        api = ApiClient.fromPrefs(this);
        // Prompt once per cold start; re-prompting on every return would
        // trap the back button if the user leaves settings unsaved.
        if (!api.isConfigured() && !settingsPrompted) {
            settingsPrompted = true;
            Toast.makeText(this, "先设置服务器地址和令牌", Toast.LENGTH_LONG).show();
            startActivity(new Intent(this, SettingsActivity.class));
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "待处理");
        menu.add(0, 2, 1, "设置");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            startActivity(new Intent(this, AttentionActivity.class));
            return true;
        }
        if (item.getItemId() == 2) {
            startActivity(new Intent(this, SettingsActivity.class));
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void send() {
        if (sending) return;
        String text = input.getText().toString().trim();
        if (text.isEmpty()) return;
        api = ApiClient.fromPrefs(this);
        if (!api.isConfigured()) {
            Toast.makeText(this, "先设置服务器地址和令牌", Toast.LENGTH_LONG).show();
            startActivity(new Intent(this, SettingsActivity.class));
            return;
        }
        input.setText("");
        messages.add(new Message(true, text));
        Message pending = new Message(false, "主智能体处理中…");
        messages.add(pending);
        adapter.notifyDataSetChanged();
        sending = true;
        sendButton.setEnabled(false);

        api.chat(text, new ApiClient.Callback<String>() {
            @Override
            public void onSuccess(String reply) {
                pending.text = reply;
                finishSend();
            }

            @Override
            public void onError(String message, String detail) {
                pending.text = message + "\n（详情：" + detail + "）";
                finishSend();
            }
        });
    }

    private void finishSend() {
        sending = false;
        sendButton.setEnabled(true);
        adapter.notifyDataSetChanged();
    }

    private final class MessageAdapter extends BaseAdapter {
        @Override public int getCount() { return messages.size(); }
        @Override public Object getItem(int i) { return messages.get(i); }
        @Override public long getItemId(int i) { return i; }
        @Override public int getViewTypeCount() { return 2; }
        @Override public int getItemViewType(int i) { return messages.get(i).user ? 0 : 1; }

        @Override
        public View getView(int i, View convertView, ViewGroup parent) {
            Message m = messages.get(i);
            if (convertView == null) {
                int layout = m.user ? R.layout.item_message_user : R.layout.item_message_agent;
                convertView = getLayoutInflater().inflate(layout, parent, false);
            }
            TextView text = convertView.findViewById(R.id.text);
            text.setText(m.text);
            return convertView;
        }
    }
}
