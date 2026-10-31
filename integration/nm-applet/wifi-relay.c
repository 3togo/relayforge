/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Optional Wi-Fi Relay controls for NetworkManager Applet. */
#include "wifi-relay.h"
#include <gio/gio.h>
#include <jansson.h>
#include <errno.h>
#include <stdio.h>

#define RELAY_NAME "io.github.erhanzeyrek.WifiHotspot"
#define RELAY_PATH "/io/github/erhanzeyrek/WifiHotspot"

struct _WifiRelay {
    gint refs;
    GDBusProxy *proxy;
    GCancellable *cancel;
    guint timer;
    guint generation;
    gboolean querying, busy, known, active, requested;
    gint64 clients;
    gchar *error;
    void (*changed) (gpointer);
    gpointer user_data;
};

typedef struct {
    WifiRelay *relay;
    guint generation;
    gboolean starting;
} PendingCall;

static void query_status (WifiRelay *relay);

static WifiRelay *
relay_ref (WifiRelay *relay)
{
    relay->refs++;
    return relay;
}

static void
relay_unref (WifiRelay *relay)
{
    if (--relay->refs)
        return;
    g_clear_object (&relay->proxy);
    g_clear_object (&relay->cancel);
    g_free (relay->error);
    g_free (relay);
}

static PendingCall *
pending_call (WifiRelay *relay, gboolean starting)
{
    PendingCall *call = g_new0 (PendingCall, 1);
    call->relay = relay_ref (relay);
    call->generation = relay->generation;
    call->starting = starting;
    return call;
}

static void
changed (WifiRelay *relay)
{
    if (relay->changed)
        relay->changed (relay->user_data);
}

static void
set_error (WifiRelay *relay, const gchar *message)
{
    g_free (relay->error);
    relay->error = g_strdup (message);
}

static gboolean
read_status (WifiRelay *relay, const gchar *text)
{
    json_t *root = json_loads (text, JSON_REJECT_DUPLICATES, NULL);
    json_t *active, *clients, *error, *desired;
    gboolean updated;
    gint64 count;
    if (!json_is_object (root)) {
        json_decref (root);
        return FALSE;
    }
    active = json_object_get (root, "active");
    desired = json_object_get (root, "desired_active");
    clients = json_object_get (root, "client_count");
    error = json_object_get (root, "error");
    if (!json_is_boolean (active) || (desired && !json_is_boolean (desired))) {
        json_decref (root);
        return FALSE;
    }
    count = json_is_integer (clients) ? MAX (0, json_integer_value (clients)) : 0;
    updated = !relay->known || relay->active != json_is_true (active)
        || relay->requested != (json_is_true (active) || json_is_true (desired))
        || relay->clients != count
        || g_strcmp0 (relay->error, json_string_value (error)) != 0;
    relay->known = TRUE;
    relay->active = json_is_true (active);
    relay->requested = relay->active || json_is_true (desired);
    relay->clients = count;
    set_error (relay, json_string_value (error));
    json_decref (root);
    if (updated)
        changed (relay);
    return TRUE;
}

static void
status_done (GObject *source, GAsyncResult *result, gpointer data)
{
    PendingCall *call = data;
    WifiRelay *relay = call->relay;
    GError *error = NULL;
    GVariant *reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    relay->querying = FALSE;
    if (!g_cancellable_is_cancelled (relay->cancel) && call->generation == relay->generation) {
        const gchar *text;
        if (reply && g_variant_is_of_type (reply, G_VARIANT_TYPE ("(s)"))) {
            g_variant_get (reply, "(&s)", &text);
            if (!read_status (relay, text)) {
                relay->known = FALSE;
                set_error (relay, "Invalid hotspot status from service");
                changed (relay);
            }
        } else {
            relay->known = FALSE;
            set_error (relay, error ? error->message : "Invalid hotspot status from service");
            changed (relay);
        }
    }
    g_clear_pointer (&reply, g_variant_unref);
    g_clear_error (&error);
    if (call->generation != relay->generation)
        query_status (relay);
    relay_unref (relay);
    g_free (call);
}

static void
query_status (WifiRelay *relay)
{
    gchar *owner;
    if (!relay->proxy || relay->querying || relay->busy
        || g_cancellable_is_cancelled (relay->cancel))
        return;
    owner = g_dbus_proxy_get_name_owner (relay->proxy);
    if (!owner) {
        if (relay->known) {
            relay->known = FALSE;
            relay->active = FALSE;
            relay->requested = FALSE;
            changed (relay);
        }
        return;
    }
    g_free (owner);
    relay->querying = TRUE;
    g_dbus_proxy_call (relay->proxy, "GetStatus", NULL, G_DBUS_CALL_FLAGS_NO_AUTO_START,
                      5000, relay->cancel, status_done, pending_call (relay, FALSE));
}

static gboolean
poll_status (gpointer data)
{
    query_status (data);
    return G_SOURCE_CONTINUE;
}

static void
owner_changed (GObject *proxy, GParamSpec *pspec, gpointer data)
{
    WifiRelay *relay = data;
    relay->generation++;
    relay->known = FALSE;
    relay->active = FALSE;
    relay->requested = FALSE;
    set_error (relay, NULL);
    changed (relay);
    query_status (relay);
}

static void
service_signal (GDBusProxy *proxy, const gchar *sender, const gchar *name,
                GVariant *parameters, gpointer data)
{
    if (g_str_equal (name, "StatusChanged"))
        query_status (data);
}

static void
proxy_ready (GObject *source, GAsyncResult *result, gpointer data)
{
    WifiRelay *relay = data;
    GError *error = NULL;
    relay->proxy = g_dbus_proxy_new_for_bus_finish (result, &error);
    if (relay->proxy && !g_cancellable_is_cancelled (relay->cancel)) {
        g_signal_connect (relay->proxy, "notify::g-name-owner", G_CALLBACK (owner_changed), relay);
        g_signal_connect (relay->proxy, "g-signal", G_CALLBACK (service_signal), relay);
        query_status (relay);
    }
    g_clear_error (&error);
    relay_unref (relay);
}

static void
show_error (const gchar *message)
{
    GtkWidget *dialog = gtk_message_dialog_new (NULL, 0, GTK_MESSAGE_ERROR,
                                               GTK_BUTTONS_CLOSE, "%s", message);
    gtk_window_set_title (GTK_WINDOW (dialog), "Wi-Fi Relay");
    g_signal_connect_swapped (dialog, "response", G_CALLBACK (gtk_widget_destroy), dialog);
    gtk_widget_show (dialog);
}

static gchar *
operation_failure (GVariant *reply, gboolean starting)
{
    if (starting) {
        const gchar *text, *message;
        json_t *root;
        gchar *failure;
        if (!reply || !g_variant_is_of_type (reply, G_VARIANT_TYPE ("(s)")))
            return g_strdup ("Invalid hotspot Start reply");
        g_variant_get (reply, "(&s)", &text);
        root = json_loads (text, JSON_REJECT_DUPLICATES, NULL);
        if (json_is_object (root) && json_is_true (json_object_get (root, "success"))) {
            json_decref (root);
            return NULL;
        }
        message = json_string_value (json_object_get (root, "error"));
        failure = g_strdup (message ? message : "Hotspot could not start. Check Wi-Fi Relay Settings.");
        json_decref (root);
        return failure;
    } else {
        gboolean success = FALSE;
        if (reply && g_variant_is_of_type (reply, G_VARIANT_TYPE ("(b)")))
            g_variant_get (reply, "(b)", &success);
        return success ? NULL : g_strdup ("Hotspot could not stop");
    }
}

static void
operation_done (GObject *source, GAsyncResult *result, gpointer data)
{
    PendingCall *call = data;
    WifiRelay *relay = call->relay;
    GError *error = NULL;
    GVariant *reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    gchar *failure = NULL;
    if (!reply)
        failure = g_strdup (error ? error->message : "Hotspot request failed");
    else
        failure = operation_failure (reply, call->starting);
    relay->busy = FALSE;
    if (!g_cancellable_is_cancelled (relay->cancel)) {
        if (failure && call->generation == relay->generation) {
            set_error (relay, failure);
            show_error (failure);
        }
        changed (relay);
        query_status (relay);
    }
    g_free (failure);
    g_clear_pointer (&reply, g_variant_unref);
    g_clear_error (&error);
    relay_unref (relay);
    g_free (call);
}

static void
activate_hotspot (GtkMenuItem *item, gpointer data)
{
    WifiRelay *relay = data;
    if (!relay->known || relay->busy)
        return;
    relay->busy = TRUE;
    changed (relay);
    g_dbus_proxy_call (relay->proxy, relay->requested ? "Stop" : "Start", NULL,
                      G_DBUS_CALL_FLAGS_NONE, 60000, relay->cancel,
                      operation_done, pending_call (relay, !relay->requested));
}

static void
open_settings (GtkMenuItem *item, gpointer data)
{
    GError *error = NULL;
    gchar *argv[] = { "wifi-hotspot-settings", NULL };
    if (!g_spawn_async (NULL, argv, NULL, G_SPAWN_SEARCH_PATH, NULL, NULL, NULL, &error)) {
        show_error (error->message);
        g_error_free (error);
    }
}

static gchar *
tray_preference_path (void)
{
    return g_build_filename (g_get_user_config_dir (), "wifi-hotspot", "tray.json", NULL);
}

static json_t *
load_tray_preferences (const gchar *path)
{
    FILE *stream = fopen (path, "rb");
    char buffer[16385];
    size_t length;
    gboolean failed;
    if (!stream)
        return NULL;
    length = fread (buffer, 1, sizeof buffer, stream);
    failed = ferror (stream) != 0;
    fclose (stream);
    if (failed || length > 16384)
        return NULL;
    return json_loadb (buffer, length, JSON_REJECT_DUPLICATES, NULL);
}

static gboolean
read_tray_visible (const gchar *path)
{
    json_t *root = load_tray_preferences (path);
    gboolean visible = !json_is_object (root) || !json_is_false (json_object_get (root, "visible"));
    json_decref (root);
    return visible;
}

static gboolean
write_tray_visible (const gchar *path, gboolean visible, GError **error)
{
    gchar *directory = g_path_get_dirname (path);
    GFile *file;
    gboolean saved;
    json_t *root;
    gchar *text;
    if (g_mkdir_with_parents (directory, 0700) != 0) {
        g_set_error (error, G_IO_ERROR, g_io_error_from_errno (errno),
                     "Could not create tray preferences directory: %s", g_strerror (errno));
        g_free (directory);
        return FALSE;
    }
    g_free (directory);
    root = load_tray_preferences (path);
    if (!json_is_object (root)) {
        json_decref (root);
        root = json_object ();
    }
    json_object_set_new (root, "visible", json_boolean (visible));
    text = json_dumps (root, JSON_COMPACT);
    json_decref (root);
    if (!text) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Could not encode tray preferences");
        return FALSE;
    }
    file = g_file_new_for_path (path);
    saved = g_file_replace_contents (file, text, strlen (text), NULL, FALSE,
                                    G_FILE_CREATE_PRIVATE, NULL, NULL, error);
    g_object_unref (file);
    free (text);
    return saved;
}

static void
toggle_tray (GtkCheckMenuItem *item, gpointer data)
{
    WifiRelay *relay = data;
    gchar *path = tray_preference_path ();
    GError *error = NULL;
    gboolean visible = gtk_check_menu_item_get_active (item);
    gboolean previous = read_tray_visible (path);
    if (write_tray_visible (path, visible, &error) && visible) {
        gchar *argv[] = { "/usr/bin/python3", "/usr/lib/relayforge/settings/tray.py", "--show-icon", NULL };
        if (!g_spawn_async (NULL, argv, NULL, 0, NULL, NULL, NULL, &error))
            write_tray_visible (path, previous, NULL);
    }
    if (error) {
        show_error (error->message);
        g_error_free (error);
    }
    g_free (path);
    changed (relay);
}

WifiRelay *
wifi_relay_new (GCallback callback, gpointer user_data)
{
    WifiRelay *relay = g_new0 (WifiRelay, 1);
    relay->refs = 1;
    relay->cancel = g_cancellable_new ();
    relay->changed = (void (*) (gpointer)) callback;
    relay->user_data = user_data;
    relay->timer = g_timeout_add_seconds (5, poll_status, relay);
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SYSTEM,
        G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START | G_DBUS_PROXY_FLAGS_DO_NOT_LOAD_PROPERTIES,
        NULL, RELAY_NAME, RELAY_PATH, RELAY_NAME, relay->cancel, proxy_ready, relay_ref (relay));
    return relay;
}

void
wifi_relay_free (WifiRelay *relay)
{
    if (!relay)
        return;
    relay->changed = NULL;
    g_source_remove (relay->timer);
    g_cancellable_cancel (relay->cancel);
    if (relay->proxy)
        g_signal_handlers_disconnect_by_data (relay->proxy, relay);
    relay_unref (relay);
}

void
wifi_relay_add_menu (WifiRelay *relay, GtkWidget *menu)
{
    GtkWidget *parent, *submenu, *item;
    gchar *label;
    if (!relay)
        return;
    parent = gtk_menu_item_new_with_label ("Wi-Fi Relay");
    submenu = gtk_menu_new ();
    gtk_menu_item_set_submenu (GTK_MENU_ITEM (parent), submenu);
    item = gtk_check_menu_item_new_with_label ("Hotspot");
    gtk_check_menu_item_set_active (GTK_CHECK_MENU_ITEM (item), relay->known && relay->requested);
    gtk_widget_set_sensitive (item, relay->known && !relay->busy);
    g_signal_connect (item, "activate", G_CALLBACK (activate_hotspot), relay);
    gtk_menu_shell_append (GTK_MENU_SHELL (submenu), item);
    label = g_strdup_printf ("%s · %" G_GINT64_FORMAT " connected",
        relay->busy ? (relay->requested ? "Stopping…" : "Starting…") :
        !relay->known ? "Service unavailable" : relay->active ? "Active" : relay->requested ? "Waiting for Wi-Fi" : "Off",
        relay->known ? relay->clients : 0);
    item = gtk_menu_item_new_with_label (label);
    g_free (label);
    gtk_widget_set_sensitive (item, FALSE);
    gtk_menu_shell_append (GTK_MENU_SHELL (submenu), item);
    if (relay->known && relay->error && *relay->error) {
        item = gtk_menu_item_new_with_label (relay->error);
        gtk_widget_set_sensitive (item, FALSE);
        gtk_menu_shell_append (GTK_MENU_SHELL (submenu), item);
    }
    gtk_menu_shell_append (GTK_MENU_SHELL (submenu), gtk_separator_menu_item_new ());
    {
        gchar *path = tray_preference_path ();
        item = gtk_check_menu_item_new_with_label ("Show Wi-Fi Relay icon");
        gtk_check_menu_item_set_active (GTK_CHECK_MENU_ITEM (item), read_tray_visible (path));
        g_free (path);
        gtk_widget_set_sensitive (item, g_file_test ("/usr/lib/relayforge/settings/tray.py", G_FILE_TEST_IS_REGULAR));
        g_signal_connect (item, "toggled", G_CALLBACK (toggle_tray), relay);
        gtk_menu_shell_append (GTK_MENU_SHELL (submenu), item);
    }
    item = gtk_menu_item_new_with_label ("Open Wi-Fi Relay Settings…");
    g_signal_connect (item, "activate", G_CALLBACK (open_settings), NULL);
    gtk_menu_shell_append (GTK_MENU_SHELL (submenu), item);
    gtk_menu_shell_append (GTK_MENU_SHELL (menu), parent);
    gtk_widget_show_all (parent);
}
