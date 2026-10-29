/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Protocol and asynchronous lifecycle tests on a private D-Bus; no host networking. */
#include "wifi-relay.c"
#include <glib/gstdio.h>

static void test_tray_visibility (void)
{
    GError *error = NULL;
    gchar *directory = g_dir_make_tmp ("relay-tray-test-XXXXXX", &error);
    gchar *path = g_build_filename (directory, "preferences", "tray.json", NULL);
    gchar *parent = g_path_get_dirname (path);
    GStatBuf st;
    g_assert_no_error (error);
    g_assert_true (read_tray_visible (path));
    g_assert_true (write_tray_visible (path, FALSE, &error));
    g_assert_no_error (error);
    g_assert_false (read_tray_visible (path));
    g_assert_cmpint (g_stat (path, &st), ==, 0);
    g_assert_cmpint (st.st_mode & 0777, ==, 0600);
    g_assert_true (g_file_set_contents (path, "{\"visible\":false,\"future\":\"keep\"}", -1, &error));
    g_assert_no_error (error);
    g_assert_true (write_tray_visible (path, TRUE, &error));
    g_assert_no_error (error);
    g_assert_true (read_tray_visible (path));
    {
        json_t *preferences = json_load_file (path, 0, NULL);
        g_assert_cmpstr (json_string_value (json_object_get (preferences, "future")), ==, "keep");
        json_decref (preferences);
    }
    {
        gchar *oversized = g_strnfill (16385, ' ');
        g_assert_true (g_file_set_contents (path, oversized, -1, &error));
        g_assert_no_error (error);
        g_assert_null (load_tray_preferences (path));
        g_assert_true (read_tray_visible (path));
        g_free (oversized);
    }
    g_file_set_contents (path, "invalid", -1, &error);
    g_assert_no_error (error);
    g_assert_true (read_tray_visible (path));
    g_assert_false (write_tray_visible (parent, FALSE, &error));
    g_assert_nonnull (error);
    g_clear_error (&error);
    g_remove (path); g_rmdir (parent); g_rmdir (directory);
    g_free (parent); g_free (path); g_free (directory);
}

static GTestDBus *bus;
static GDBusNodeInfo *info;

typedef struct {
    GDBusConnection *connection;
    guint registration, starts, stops, updates, active_updates;
    gboolean active, hold_status, hold_operation, deny_start;
    GDBusMethodInvocation *held;
    WifiRelay *relay;
} Fixture;

static void pump (void)
{
    while (g_main_context_iteration (NULL, FALSE))
        ;
    g_usleep (1000);
}

static void fake_call (GDBusConnection *connection, const gchar *sender, const gchar *path,
                       const gchar *interface, const gchar *method, GVariant *args,
                       GDBusMethodInvocation *invocation, gpointer data)
{
    Fixture *f = data;
    if (g_str_equal (method, "Start")) {
        f->starts++;
        if (f->hold_operation) {
            f->held = g_object_ref (invocation);
            return;
        }
        if (f->deny_start) {
            g_dbus_method_invocation_return_dbus_error (invocation, "org.example.Denied", "Permission denied");
            return;
        }
        f->active = TRUE;
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", "{\"success\":true}"));
    } else if (g_str_equal (method, "Stop")) {
        f->active = FALSE;f->stops++;
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(b)", TRUE));
    } else if (f->hold_status) {
        g_assert_null (f->held);
        f->held = g_object_ref (invocation);
    } else {
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)",
            f->active ? "{\"active\":true,\"client_count\":2}" : "{\"active\":false,\"client_count\":0}"));
    }
}
static const GDBusInterfaceVTable vtable = { .method_call = fake_call };

static void name_call (Fixture *f, gboolean acquire)
{
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_sync (f->connection, "org.freedesktop.DBus",
        "/org/freedesktop/DBus", "org.freedesktop.DBus", acquire ? "RequestName" : "ReleaseName",
        acquire ? g_variant_new ("(su)", RELAY_NAME, 0) : g_variant_new ("(s)", RELAY_NAME),
        NULL, 0, 5000, NULL, &error);
    g_assert_no_error (error);
    g_variant_unref (reply);
}

static void start_service (Fixture *f)
{
    GError *error = NULL;
    f->connection = g_dbus_connection_new_for_address_sync (g_test_dbus_get_bus_address (bus),
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, &error);
    g_assert_no_error (error);
    f->registration = g_dbus_connection_register_object (f->connection, RELAY_PATH,
        info->interfaces[0], &vtable, f, NULL, &error);
    g_assert_no_error (error);
    name_call (f, TRUE);
}

static void count_update (gpointer data)
{
    Fixture *f = data;
    f->updates++;
    if (f->relay && f->relay->known && f->relay->active)
        f->active_updates++;
}

static void wait_for (WifiRelay *relay, gboolean active)
{
    gint64 deadline = g_get_monotonic_time () + 5000000;
    while ((!relay->known || relay->active != active || relay->busy) && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_true (relay->known);
    g_assert_cmpint (relay->active, ==, active);
    g_assert_false (relay->busy);
}

static void setup (Fixture *f, gconstpointer data)
{
    start_service (f);
    f->relay = wifi_relay_new (G_CALLBACK (count_update), f);
    wait_for (f->relay, FALSE);
}

static void teardown (Fixture *f, gconstpointer data)
{
    wifi_relay_free (f->relay);
    if (f->held) {
        g_dbus_method_invocation_return_dbus_error (f->held, "org.example.Cancelled", "Test complete");
        g_clear_object (&f->held);
    }
    name_call (f, FALSE);
    g_dbus_connection_unregister_object (f->connection, f->registration);
    g_dbus_connection_close_sync (f->connection, NULL, NULL);
    g_object_unref (f->connection);
    for (guint i = 0; i < 20; i++)
        pump ();
}

static GtkWidget *menu_toggle (WifiRelay *relay, GtkWidget **menu)
{
    GList *items, *children;
    GtkWidget *submenu, *toggle;
    *menu = gtk_menu_new ();g_object_ref_sink (*menu);
    wifi_relay_add_menu (relay, *menu);
    items = gtk_container_get_children (GTK_CONTAINER (*menu));
    g_assert_cmpstr (gtk_menu_item_get_label (items->data), ==, "Wi-Fi Relay");
    submenu = gtk_menu_item_get_submenu (items->data);
    children = gtk_container_get_children (GTK_CONTAINER (submenu));
    toggle = children->data;
    g_list_free (children);g_list_free (items);
    return toggle;
}

static void destroy_menu (GtkWidget *menu)
{
    gtk_widget_destroy (menu);g_object_unref (menu);
}

static void test_start_stop (Fixture *f, gconstpointer data)
{
    GtkWidget *menu, *toggle = menu_toggle (f->relay, &menu);
    g_assert_true (gtk_widget_get_sensitive (toggle));
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));
    g_assert_true (f->relay->busy);
    /* Even a queued second activation must not issue a duplicate request. */
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));
    destroy_menu (menu);
    toggle = menu_toggle (f->relay, &menu);
    g_assert_false (gtk_widget_get_sensitive (toggle));
    destroy_menu (menu);
    wait_for (f->relay, TRUE);
    g_assert_cmpuint (f->starts, ==, 1);
    g_assert_cmpint (f->relay->clients, ==, 2);
    toggle = menu_toggle (f->relay, &menu);
    g_assert_true (gtk_check_menu_item_get_active (GTK_CHECK_MENU_ITEM (toggle)));
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));destroy_menu (menu);
    wait_for (f->relay, FALSE);
    g_assert_cmpuint (f->stops, ==, 1);
}

static void test_service_restart (Fixture *f, gconstpointer data)
{
    gint64 deadline = g_get_monotonic_time () + 1000000;
    GtkWidget *menu, *toggle;
    name_call (f, FALSE);
    while (f->relay->known && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_false (f->relay->known);
    toggle = menu_toggle (f->relay, &menu);
    g_assert_false (gtk_widget_get_sensitive (toggle));
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));
    g_assert_cmpuint (f->starts, ==, 0);destroy_menu (menu);
    name_call (f, TRUE);
    wait_for (f->relay, FALSE);
}

static void hold_reply (Fixture *f)
{
    gint64 deadline = g_get_monotonic_time () + 1000000;
    f->hold_status = TRUE;
    query_status (f->relay);
    while (!f->held && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_nonnull (f->held);
}

static void test_stale_owner_reply (Fixture *f, gconstpointer data)
{
    Fixture replacement = { 0 };
    guint generation = f->relay->generation;
    gint64 deadline = g_get_monotonic_time () + 1000000;
    hold_reply (f);
    name_call (f, FALSE);start_service (&replacement);
    while (f->relay->generation == generation && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_cmpuint (f->relay->generation, >, generation);
    f->active_updates = 0;
    g_dbus_method_invocation_return_value (f->held,
        g_variant_new ("(s)", "{\"active\":true,\"client_count\":99}"));
    g_clear_object (&f->held);
    wait_for (f->relay, FALSE);
    g_assert_cmpuint (f->active_updates, ==, 0);
    g_assert_cmpint (f->relay->clients, ==, 0);
    teardown (&replacement, NULL);
}

static void test_free_during_status (Fixture *f, gconstpointer data)
{
    WifiRelay *relay = relay_ref (f->relay);
    guint updates = f->updates;
    hold_reply (f);
    wifi_relay_free (f->relay);f->relay = NULL;
    g_dbus_method_invocation_return_value (f->held,
        g_variant_new ("(s)", "{\"active\":true,\"client_count\":2}"));
    g_clear_object (&f->held);
    for (guint i = 0; i < 100 && relay->querying; i++)
        pump ();
    g_assert_false (relay->querying);
    g_assert_cmpuint (f->updates, ==, updates);
    g_assert_cmpint (relay->refs, ==, 1);
    relay_unref (relay);
}

static void test_status_signal (Fixture *f, gconstpointer data)
{
    f->active = TRUE;
    g_dbus_connection_emit_signal (f->connection, NULL, RELAY_PATH, RELAY_NAME,
        "StatusChanged", g_variant_new ("(s)", "{\"active\":true}"), NULL);
    wait_for (f->relay, TRUE);
}

static guint close_error_dialogs (void)
{
    GList *windows = gtk_window_list_toplevels ();
    guint dialogs = 0;
    for (GList *item = windows; item; item = item->next) {
        if (GTK_IS_MESSAGE_DIALOG (item->data)) {
            dialogs++;
            gtk_widget_destroy (item->data);
        }
    }
    g_list_free (windows);
    return dialogs;
}

static void start_held_operation (Fixture *f)
{
    GtkWidget *menu, *toggle;
    gint64 deadline = g_get_monotonic_time () + 1000000;
    f->hold_operation = TRUE;
    toggle = menu_toggle (f->relay, &menu);
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));destroy_menu (menu);
    while (!f->held && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_nonnull (f->held);
}

static void test_start_denied (Fixture *f, gconstpointer data)
{
    GtkWidget *menu, *toggle;
    f->deny_start = TRUE;
    toggle = menu_toggle (f->relay, &menu);
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));destroy_menu (menu);
    wait_for (f->relay, FALSE);
    g_assert_cmpuint (f->starts, ==, 1);
    g_assert_cmpuint (close_error_dialogs (), ==, 1);
}

static void test_free_during_operation (Fixture *f, gconstpointer data)
{
    WifiRelay *relay = relay_ref (f->relay);
    guint updates = f->updates;
    start_held_operation (f);
    /* Starting itself emits one update before cancellation. */
    updates = f->updates;
    wifi_relay_free (f->relay);f->relay = NULL;
    g_dbus_method_invocation_return_dbus_error (f->held, "org.example.Denied", "Permission denied");
    g_clear_object (&f->held);
    for (guint i = 0; i < 100 && relay->busy; i++)
        pump ();
    g_assert_false (relay->busy);
    g_assert_cmpuint (f->updates, ==, updates);
    g_assert_cmpint (relay->refs, ==, 1);
    g_assert_cmpuint (close_error_dialogs (), ==, 0);
    relay_unref (relay);
}

static void test_stale_operation (Fixture *f, gconstpointer data)
{
    Fixture replacement = { 0 };
    guint generation = f->relay->generation;
    gint64 deadline = g_get_monotonic_time () + 1000000;
    start_held_operation (f);
    name_call (f, FALSE);start_service (&replacement);
    while (f->relay->generation == generation && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_cmpuint (f->relay->generation, >, generation);
    g_dbus_method_invocation_return_dbus_error (f->held, "org.example.Denied", "Old service failure");
    g_clear_object (&f->held);
    wait_for (f->relay, FALSE);
    g_assert_null (f->relay->error);
    g_assert_cmpuint (close_error_dialogs (), ==, 0);
    teardown (&replacement, NULL);
}

static void test_free_before_proxy_ready (void)
{
    WifiRelay *relay = wifi_relay_new (NULL, NULL);
    relay_ref (relay);wifi_relay_free (relay);
    for (guint i = 0; i < 100 && relay->refs > 1; i++)
        pump ();
    g_assert_cmpint (relay->refs, ==, 1);
    g_assert_false (relay->querying);
    relay_unref (relay);
}

static void test_invalid_status (void)
{
    const gchar *invalid[] = { "bad", "[]", "null", "{}", "{\"active\":\"false\"}",
        "{\"active\":true,\"active\":false}" };
    WifiRelay relay = { .known = TRUE, .active = TRUE, .clients = 3 };
    for (guint i = 0; i < G_N_ELEMENTS (invalid); i++) {
        g_assert_false (read_status (&relay, invalid[i]));
        g_assert_true (relay.active);
        g_assert_cmpint (relay.clients, ==, 3);
    }
}

static void test_normalized_count (void)
{
    Fixture f = { 0 };
    WifiRelay relay = { .known = TRUE, .changed = count_update, .user_data = &f };
    g_assert_true (read_status (&relay, "{\"active\":false,\"client_count\":-1}"));
    g_assert_cmpint (relay.clients, ==, 0);
    g_assert_cmpuint (f.updates, ==, 0);
    g_free (relay.error);
}

static void test_status_changes (void)
{
    Fixture f = { 0 };
    WifiRelay relay = { .changed = count_update, .user_data = &f };
    const gchar *status = "{\"active\":true,\"client_count\":3,\"error\":null}";
    g_assert_true (read_status (&relay, status));
    g_assert_cmpuint (f.updates, ==, 1);
    g_assert_true (read_status (&relay, status));
    g_assert_cmpuint (f.updates, ==, 1);
    g_assert_true (read_status (&relay, "{\"active\":false,\"error\":\"Channel changed\"}"));
    g_assert_cmpstr (relay.error, ==, "Channel changed");
    g_assert_cmpint (relay.clients, ==, 0);
    g_assert_cmpuint (f.updates, ==, 2);g_free (relay.error);
}

static void test_waiting_status (void)
{
    WifiRelay relay = { 0 };
    g_assert_true (read_status (&relay, "{\"active\":false,\"desired_active\":true}"));
    g_assert_false (relay.active);
    g_assert_true (relay.requested);
    g_assert_false (read_status (&relay, "{\"active\":false,\"desired_active\":\"yes\"}"));
    g_assert_true (relay.requested);
    g_assert_true (read_status (&relay, "{\"active\":false,\"desired_active\":false}"));
    g_assert_false (relay.requested);
    g_free (relay.error);
}

static void test_cancel_waiting (Fixture *f, gconstpointer data)
{
    GtkWidget *menu, *toggle;
    read_status (f->relay, "{\"active\":false,\"desired_active\":true}");
    toggle = menu_toggle (f->relay, &menu);
    g_assert_true (gtk_check_menu_item_get_active (GTK_CHECK_MENU_ITEM (toggle)));
    g_assert_true (gtk_widget_get_sensitive (toggle));
    gtk_menu_item_activate (GTK_MENU_ITEM (toggle));
    wait_for (f->relay, FALSE);
    g_assert_cmpuint (f->stops, ==, 1);
    g_assert_cmpuint (f->starts, ==, 0);
    gint64 deadline = g_get_monotonic_time () + 5000000;
    while (f->relay->requested && g_get_monotonic_time () < deadline)
        pump ();
    g_assert_false (f->relay->requested);
    destroy_menu (menu);
}

static void test_start_reply (void)
{
    const gchar *invalid[] = { "bad", "[]", "null", "{}", "{\"success\":\"true\"}",
        "{\"success\":true,\"success\":false}" };
    GVariant *reply = g_variant_ref_sink (g_variant_new ("(s)", "{\"success\":true}"));
    g_assert_null (operation_failure (reply, TRUE));g_variant_unref (reply);
    for (guint i = 0; i < G_N_ELEMENTS (invalid); i++) {
        gchar *failure;
        reply = g_variant_ref_sink (g_variant_new ("(s)", invalid[i]));
        failure = operation_failure (reply, TRUE);
        g_assert_nonnull (failure);g_free (failure);g_variant_unref (reply);
    }
    reply = g_variant_ref_sink (g_variant_new ("(s)", "{\"success\":false,\"error\":\"Permission denied\"}"));
    gchar *failure = operation_failure (reply, TRUE);
    g_assert_cmpstr (failure, ==, "Permission denied");g_free (failure);g_variant_unref (reply);
}

static void test_reply_types (void)
{
    GVariant *boolean = g_variant_ref_sink (g_variant_new ("(b)", TRUE));
    GVariant *json = g_variant_ref_sink (g_variant_new ("(s)", "{\"success\":true}"));
    gchar *failure = operation_failure (boolean, TRUE);
    g_assert_nonnull (failure);g_free (failure);
    failure = operation_failure (json, FALSE);
    g_assert_nonnull (failure);g_free (failure);
    g_assert_null (operation_failure (boolean, FALSE));
    g_variant_unref (boolean);g_variant_unref (json);
    boolean = g_variant_ref_sink (g_variant_new ("(b)", FALSE));
    failure = operation_failure (boolean, FALSE);
    g_assert_nonnull (failure);g_free (failure);g_variant_unref (boolean);
}

int main (int argc, char **argv)
{
    GError *error = NULL;
    g_test_init (&argc, &argv, NULL);
    gtk_init (&argc, &argv);
    bus = g_test_dbus_new (G_TEST_DBUS_NONE);g_test_dbus_up (bus);
    g_setenv ("DBUS_SYSTEM_BUS_ADDRESS", g_test_dbus_get_bus_address (bus), TRUE);
    info = g_dbus_node_info_new_for_xml (
        "<node><interface name='" RELAY_NAME "'>"
        "<method name='GetStatus'><arg type='s' direction='out'/></method>"
        "<method name='Start'><arg type='s' direction='out'/></method>"
        "<method name='Stop'><arg type='b' direction='out'/></method>"
        "<signal name='StatusChanged'><arg type='s'/></signal>"
        "</interface></node>", &error);
    g_assert_no_error (error);
    g_test_add_func ("/relay/status/invalid", test_invalid_status);
    g_test_add_func ("/relay/tray/visibility", test_tray_visibility);
    g_test_add_func ("/relay/status/count-normalization", test_normalized_count);
    g_test_add_func ("/relay/status/changes", test_status_changes);
    g_test_add_func ("/relay/replies/start", test_start_reply);
    g_test_add_func ("/relay/replies/types", test_reply_types);
    g_test_add ("/relay/menu/start-stop", Fixture, NULL, setup, test_start_stop, teardown);
    g_test_add ("/relay/menu/service-restart", Fixture, NULL, setup, test_service_restart, teardown);
    g_test_add ("/relay/menu/stale-owner-reply", Fixture, NULL, setup, test_stale_owner_reply, teardown);
    g_test_add ("/relay/menu/free-during-status", Fixture, NULL, setup, test_free_during_status, teardown);
    g_test_add ("/relay/menu/status-signal", Fixture, NULL, setup, test_status_signal, teardown);
    g_test_add ("/relay/menu/start-denied", Fixture, NULL, setup, test_start_denied, teardown);
    g_test_add ("/relay/menu/free-during-operation", Fixture, NULL, setup, test_free_during_operation, teardown);
    g_test_add ("/relay/menu/stale-operation", Fixture, NULL, setup, test_stale_operation, teardown);
    g_test_add_func ("/relay/menu/free-before-proxy-ready", test_free_before_proxy_ready);
    g_test_add_func ("/relay/status/waiting", test_waiting_status);
    g_test_add ("/relay/menu/cancel-waiting", Fixture, NULL, setup, test_cancel_waiting, teardown);
    gint result = g_test_run ();
    GDBusConnection *client = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);
    g_dbus_connection_set_exit_on_close (client, FALSE);
    g_dbus_connection_close_sync (client, NULL, NULL);g_object_unref (client);
    g_dbus_node_info_unref (info);g_test_dbus_down (bus);g_object_unref (bus);
    return result;
}
