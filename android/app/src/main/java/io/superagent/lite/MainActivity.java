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

import org.json.JSONArray;
import org.json.JSONObject;

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
    private TextView streamStatus;
    private ApiClient api;
    private boolean sending = false;
    private boolean settingsPrompted = false;
    // Events arrive on the ApiClient executor; the flag tells the main
    // thread whether a fallback retry is still possible.
    private volatile boolean streamSawEvent = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        ListView list = findViewById(R.id.messages);
        adapter = new MessageAdapter();
        list.setAdapter(adapter);
        input = findViewById(R.id.input);
        sendButton = findViewById(R.id.send);
        streamStatus = findViewById(R.id.stream_status);
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
        streamSawEvent = false;
        setStreamStatus("");

        api.chatStream(text, new ApiClient.StreamListener() {
            @Override
            public void onEvent(String type, JSONObject event) {
                streamSawEvent = true;
                runOnUiThread(() -> showStreamEvent(type, event));
            }
        }, new ApiClient.Callback<JSONObject>() {
            @Override
            public void onSuccess(JSONObject event) {
                setStreamStatus("");
                final String reply = event.optString("reply", "");
                if ("error".equals(event.optString("type", ""))) {
                    pending.text = reply.isEmpty() ? "服务器返回了错误" : reply;
                    finishSend();
                    return;
                }
                pending.text = reply.isEmpty() ? "（主智能体没有返回内容）" : reply;
                finishSend();
            }

            @Override
            public void onError(String message, String detail) {
                setStreamStatus("");
                if (streamSawEvent) {
                    // The stream died mid-flight: report it where the reply goes.
                    pending.text = message + "\n（详情：" + detail + "）";
                    finishSend();
                    return;
                }
                // Nothing arrived (old server, proxy blocking SSE): use /chat.
                pending.text = "主智能体处理中…";
                adapter.notifyDataSetChanged();
                fallbackChat(text, pending);
            }
        });
    }

    /** Retry through the blocking /chat endpoint when the stream never started. */
    private void fallbackChat(String text, Message pending) {
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

    /** One quiet line above the composer, mirroring the server's progress. */
    private void showStreamEvent(String type, JSONObject event) {
        switch (type) {
            case "accepted":
                setStreamStatus("已收到");
                break;
            case "planning":
                setStreamStatus("正在规划…");
                break;
            case "tickets":
                setStreamStatus("已派单 " + ticketCount(event) + " 张");
                break;
            case "ticket": {
                String id = event.optString("id", "");
                String shortId = id.length() > 8 ? id.substring(0, 8) : id;
                setStreamStatus("工单 #" + shortId + " " + statusLabel(event.optString("status", "")));
                break;
            }
            case "done":
                setStreamStatus("");
                break;
            default:
                // error and anything unknown: the callback clears the line.
                break;
        }
    }

    private static int ticketCount(JSONObject event) {
        JSONArray ids = event.optJSONArray("ticket_ids");
        if (ids != null) return ids.length();
        return event.optInt("count", 0);
    }

    private static String statusLabel(String status) {
        switch (status) {
            case "pending": return "排队中";
            case "running": return "执行中";
            case "awaiting_approval": return "等待你批准";
            case "needs_attention": return "需要你关注";
            case "done": return "已完成";
            case "failed": return "失败";
            default: return status;
        }
    }

    private void setStreamStatus(String text) {
        if (streamStatus == null) return;
        if (text.isEmpty()) {
            streamStatus.setVisibility(View.GONE);
        } else {
            streamStatus.setText(text);
            streamStatus.setVisibility(View.VISIBLE);
        }
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
