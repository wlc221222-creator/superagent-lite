package io.superagent.lite;

import android.app.Activity;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;

/**
 * Things waiting for the user: approvals first, each row carries its
 * action inline (DESIGN-BRIEF: quiet by default, problems actionable).
 */
public final class AttentionActivity extends Activity {

    private final List<ApiClient.Ticket> tickets = new ArrayList<>();
    private AttentionAdapter adapter;
    private TextView empty;
    private ApiClient api;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_attention);
        ListView list = findViewById(R.id.list);
        adapter = new AttentionAdapter();
        list.setAdapter(adapter);
        empty = findViewById(R.id.empty);
    }

    @Override
    protected void onResume() {
        super.onResume();
        api = ApiClient.fromPrefs(this);
        load();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "刷新");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            load();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void load() {
        api.attention(new ApiClient.Callback<List<ApiClient.Ticket>>() {
            @Override
            public void onSuccess(List<ApiClient.Ticket> value) {
                tickets.clear();
                tickets.addAll(value);
                adapter.notifyDataSetChanged();
                empty.setVisibility(tickets.isEmpty() ? View.VISIBLE : View.GONE);
            }

            @Override
            public void onError(String message, String detail) {
                Toast.makeText(AttentionActivity.this, message, Toast.LENGTH_LONG).show();
            }
        });
    }

    private void act(ApiClient.Ticket ticket, boolean approve) {
        ApiClient.Callback<Void> cb = new ApiClient.Callback<Void>() {
            @Override
            public void onSuccess(Void value) {
                Toast.makeText(AttentionActivity.this,
                        approve ? "已批准" : "已驳回", Toast.LENGTH_SHORT).show();
                load();
            }

            @Override
            public void onError(String message, String detail) {
                Toast.makeText(AttentionActivity.this, message, Toast.LENGTH_LONG).show();
            }
        };
        if (approve) api.approve(ticket.id, cb); else api.reject(ticket.id, cb);
    }

    private static String statusLabel(String status) {
        // Labels say whom the row waits for / what it needs, not raw codes.
        switch (status) {
            case "awaiting_approval": return "等待你批准";
            case "needs_attention": return "需要你关注";
            default: return status;
        }
    }

    private final class AttentionAdapter extends BaseAdapter {
        @Override public int getCount() { return tickets.size(); }
        @Override public Object getItem(int i) { return tickets.get(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View convertView, ViewGroup parent) {
            ApiClient.Ticket t = tickets.get(i);
            if (convertView == null) {
                convertView = getLayoutInflater().inflate(R.layout.item_attention, parent, false);
            }
            TextView desc = convertView.findViewById(R.id.desc);
            TextView status = convertView.findViewById(R.id.status);
            View actions = convertView.findViewById(R.id.actions);
            Button approve = convertView.findViewById(R.id.approve);
            Button reject = convertView.findViewById(R.id.reject);

            desc.setText(t.description.isEmpty() ? t.id : t.description);
            status.setText(statusLabel(t.status));
            boolean actionable = "awaiting_approval".equals(t.status);
            actions.setVisibility(actionable ? View.VISIBLE : View.GONE);
            if (actionable) {
                approve.setOnClickListener(v -> act(t, true));
                reject.setOnClickListener(v -> act(t, false));
            }
            return convertView;
        }
    }
}
