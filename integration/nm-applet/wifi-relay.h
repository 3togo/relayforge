/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <gtk/gtk.h>

typedef struct _WifiRelay WifiRelay;
WifiRelay *wifi_relay_new (GCallback changed, gpointer user_data);
void wifi_relay_free (WifiRelay *relay);
void wifi_relay_add_menu (WifiRelay *relay, GtkWidget *menu);
