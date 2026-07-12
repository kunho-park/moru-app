/**
 * Auto-update wiring (electron-updater, GitHub Releases feed).
 * State is mirrored to every renderer via the "updates:state" channel.
 */

import { BrowserWindow, app, ipcMain } from "electron";
import electronUpdater from "electron-updater";

import type { UpdateState } from "../shared/bridge";

const { autoUpdater } = electronUpdater;

let state: UpdateState = { status: "idle" };

function setState(next: UpdateState): void {
  state = next;
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send("updates:state", state);
  }
}

export function registerUpdater(): void {
  ipcMain.handle("updates:get-state", () => state);
  ipcMain.handle("updates:check", async () => {
    if (!app.isPackaged) {
      // dev builds have no update feed; report a terminal "none"
      setState({ status: "none" });
      return;
    }
    setState({ status: "checking" });
    try {
      await autoUpdater.checkForUpdates();
    } catch (error) {
      setState({ status: "error", message: String(error) });
    }
  });
  ipcMain.on("updates:install", () => {
    if (state.status === "ready") autoUpdater.quitAndInstall();
  });

  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.on("update-available", (info) => {
    setState({ status: "available", version: info.version });
  });
  autoUpdater.on("download-progress", (progress) => {
    const version = state.status === "available" || state.status === "downloading" ? state.version : "";
    setState({ status: "downloading", percent: progress.percent, version });
  });
  autoUpdater.on("update-downloaded", (info) => {
    setState({ status: "ready", version: info.version });
  });
  autoUpdater.on("update-not-available", () => {
    setState({ status: "none" });
  });
  autoUpdater.on("error", (error) => {
    setState({ status: "error", message: String(error) });
  });
  // fire-and-forget startup check
  void autoUpdater.checkForUpdates().catch(() => setState({ status: "none" }));
}
