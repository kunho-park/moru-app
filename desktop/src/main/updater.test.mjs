import { EventEmitter } from "node:events";

import { expect, mock, test } from "bun:test";

const ipcHandlers = new Map();
const checkForUpdates = mock(() => new Promise(() => {}));
const autoUpdater = Object.assign(new EventEmitter(), {
  autoDownload: false,
  checkForUpdates,
  quitAndInstall: mock(() => {}),
});

mock.module("electron", () => ({
  BrowserWindow: { getAllWindows: () => [] },
  app: { isPackaged: true },
  ipcMain: {
    handle: mock((channel, handler) => ipcHandlers.set(channel, handler)),
    on: mock(() => {}),
  },
}));
mock.module("electron-updater", () => ({ default: { autoUpdater } }));

const { registerUpdater } = await import("./updater.ts?startup-update-test");

test("starts a packaged-app update check during updater registration", async () => {
  registerUpdater();

  expect(autoUpdater.autoDownload).toBeTrue();
  expect(checkForUpdates).toHaveBeenCalledTimes(1);
  expect(ipcHandlers.has("updates:get-state")).toBeTrue();
  expect(await ipcHandlers.get("updates:get-state")()).toEqual({ status: "checking" });
});
