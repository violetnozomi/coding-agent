import o from "ospec"
import n, {Mocked} from "../nodemocker"
import {DesktopDownloadManager} from "../../../src/desktop/DesktopDownloadManager"
import {assertThrows} from "@tutao/tutanota-test-utils"
import {CancelledError} from "../../../src/api/common/error/CancelledError"
import {delay} from "@tutao/tutanota-utils"
import {DesktopNetworkClient} from "../../../src/desktop/DesktopNetworkClient"
import {PreconditionFailedError, TooManyRequestsError} from "../../../src/api/common/error/RestError"
import type * as fs from "fs"

const DEFAULT_DOWNLOAD_PATH = "/a/download/path/"

o.spec("DesktopDownloadManagerTest", function () {
	let conf
	let session
	let item
	let WriteStream: Mocked<fs.WriteStream>
	let fs
	let dateProvider
	let time = 1629115820468

	const standardMocks = () => {
		conf = {
			removeListener: (key: string, cb: () => void) => n.spyify(conf),
			on: (key: string) => n.spyify(conf),
			getVar: (key: string) => {
				switch (key) {
					case "defaultDownloadPath":
						return DEFAULT_DOWNLOAD_PATH

					default:
						throw new Error(`unexpected getVar key ${key}`)
				}
			},
			setVar: (key: string, val: any) => {
			},
			getConst: (key: string) => {
				switch (key) {
					case "fileManagerTimeout":
						return 30

					default:
						throw new Error(`unexpected getConst key ${key}`)
				}
			},
		}
		const electron = {
			dialog: {
				showMessageBox: () =>
					Promise.resolve({
						response: 1,
					}),
				showSaveDialog: () =>
					Promise.resolve({
						filePath: "parentDir/resultFilePath",
					}),
			},
			shell: {
				openPath: path => Promise.resolve(path !== "invalid" ? "" : "invalid path"),
			},
			app: {
				getPath: () => "/some/path/",
			},
		}
		session = {
			callbacks: {},
			removeAllListeners: function () {
				this.callbacks = {}
				return this
			},
			setSpellCheckerDictionaryDownloadURL: () => {
			},
			on: function (ev, cb) {
				this.callbacks[ev] = cb
				return this
			},
		}
		const net = {
			request(url, opts) {
				return new net.Request()
			},
			Request: n.classify({
				prototype: {
					callbacks: {},
					on: function (ev, cb) {
						this.callbacks[ev] = cb
						return this
					},
					end: function () {
						if (this.callbacks["end"]) this.callbacks["end"]()
					},
					abort: function () {
						if (this.callbacks["error"]) this.callbacks["error"](new Error("aborted"))
					},
				},
				statics: {},
			}),
			Response: n.classify({
				prototype: {
					constructor: function (statusCode) {
						this.statusCode = statusCode
						this.headers = {}
					},
					callbacks: {},
					on: function (ev, cb) {
						this.callbacks[ev] = cb
						return this
					},
					setEncoding: function (enc) {
					},
					destroy: function (e) {
						this.callbacks["error"](e)
					},
					pipe: function (dest, opts) {
						if (dest && typeof dest.end === "function") {
							dest.end()
						}
						return this
					},
					headers: {},
				},
				statics: {},
			}),
		} as const
		item = {
			callbacks: {},
			savePath: "NOT SET!",
			on: function (ev, cb) {
				this.callbacks[ev] = cb
				return this
			},
			getFilename: () => "/this/is/a-file?.name",
		}
		WriteStream = n.classify({
			prototype: {
				callbacks: {},
				on: function (ev, cb) {
					this.callbacks[ev] = cb
					return this
				},
				close: function () {
					this.callbacks["close"]()
				},
				removeAllListeners: function (ev) {
					this.callbacks[ev] = () => {
					}

					return this
				},
				end: function () {
					this.callbacks["finish"]()
				},
			},
			statics: {},
		})
		fs = {
			closeSync: () => {
			},
			openSync: () => {
			},
			writeFile: () => Promise.resolve(),
			createWriteStream: () => new WriteStream(),
			existsSync: path => path === DEFAULT_DOWNLOAD_PATH,
			mkdirSync: () => {
			},
			promises: {
				unlink: () => Promise.resolve(),
				mkdir: () => Promise.resolve(),
				writeFile: () => Promise.resolve(),
				readdir: () => Promise.resolve([]),
			},
		}
		const lang = {
			get: key => key,
		}
		const desktopUtils = {
			touch: path => {
			},
			getTutanotaTempPath: (...subdirs) => "/tutanota/tmp/path/" + subdirs.join("/"),
		}
		dateProvider = {
			now: () => time,
		}
		return {
			netMock: n.mock<typeof DesktopNetworkClient & Writeable<typeof net>>("__net", net).set(),
			confMock: n.mock("__conf", conf).set(),
			electronMock: n.mock<typeof import("electron")>("electron", electron).set(),
			fsMock: n.mock<typeof import("fs")>("fs-extra", fs).set(),
			desktopUtilsMock: n.mock("./DesktopUtils", desktopUtils).set(),
			langMock: n.mock("../misc/LanguageViewModel", lang).set(),
			dateProviderMock: n.mock("__dateProvider", dateProvider).set(),
		}
	}

	function makeMockedDownloadManager({electronMock, desktopUtilsMock, confMock, netMock, fsMock, dateProviderMock}) {
		return new DesktopDownloadManager(confMock, netMock, desktopUtilsMock, dateProviderMock, fsMock, electronMock)
	}

	o.spec("saveBlob", function () {
		o("no default download path => save to user selected path", async function () {
			const mocks = standardMocks()
			mocks.confMock = n
				.mock("__conf", conf)
				.with({
					getVar: key => {
						switch (key) {
							case "defaultDownloadPath":
								return null

							default:
								throw new Error(`unexpected getVar key ${key}`)
						}
					},
				})
				.set()
			const dl = makeMockedDownloadManager(mocks)
			await dl.saveBlob("blob", new Uint8Array([1]))
			o(mocks.fsMock.promises.mkdir.args).deepEquals([
				"parentDir",
				{
					recursive: true,
				},
			])
			o(mocks.fsMock.promises.writeFile.args[0]).equals("parentDir/resultFilePath")
		})

		o("no default download path, cancelled", async function () {
			const mocks = standardMocks()
			mocks.confMock = n
				.mock("__conf", conf)
				.with({
					getVar: key => {
						switch (key) {
							case "defaultDownloadPath":
								return null

							default:
								throw new Error(`unexpected getVar key ${key}`)
						}
					},
				})
				.set()

			mocks.electronMock.dialog.showSaveDialog = () =>
				Promise.resolve({
					canceled: true,
				})

			const dl = makeMockedDownloadManager(mocks)
			await assertThrows(CancelledError, () => dl.saveBlob("blob", new Uint8Array([1])))
		})

		o("with default download path", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			await dl.saveBlob("blob", new Uint8Array([1]))
			o(mocks.fsMock.promises.mkdir.args).deepEquals([
				"/a/download/path",
				{
					recursive: true,
				},
			])
			o(mocks.fsMock.promises.writeFile.args[0]).equals("/a/download/path/blob")
			o(mocks.electronMock.shell.openPath.callCount).equals(1)
			o(mocks.electronMock.shell.openPath.args[0]).equals("/a/download/path")
		})

		o("with default download path but file exists", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)

			mocks.fsMock.promises.readdir = () => Promise.resolve(["blob"] as any)

			await dl.saveBlob("blob", new Uint8Array([1]))
			o(mocks.fsMock.promises.mkdir.args).deepEquals([
				"/a/download/path",
				{
					recursive: true,
				},
			])
			o(mocks.fsMock.promises.writeFile.args[0]).equals("/a/download/path/blob-1")
			o(mocks.electronMock.shell.openPath.callCount).equals(1)
			o(mocks.electronMock.shell.openPath.args[0]).equals("/a/download/path")
		})

		o("two downloads, open two filemanagers", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			await dl.saveBlob("blob", new Uint8Array([0]))
			o(mocks.electronMock.shell.openPath.callCount).equals(1)
			await dl.saveBlob("blob", new Uint8Array([0]))
			o(mocks.electronMock.shell.openPath.callCount).equals(1)
		})

		o("two downloads, open two filemanagers after a pause", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			await dl.saveBlob("blob", new Uint8Array([0]))
			o(mocks.electronMock.shell.openPath.callCount).equals(1)
			time += 1000 * 60
			await dl.saveBlob("blob", new Uint8Array([0]))
			o(mocks.electronMock.shell.openPath.callCount).equals(2)
		})
	})

	o.spec("downloadNative", async function () {
		o("no error", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			const response = new mocks.netMock.Response(200)
			// simulate the transfer completing: piping into the write stream ends it
			response.pipe = o.spy((dest, opts) => {
				dest.end()
				return response
			})

			const promise = dl.downloadNative("some://url/file", "nativelyDownloadedFile", {
				v: "foo",
				accessToken: "bar",
			})
			await delay(1)

			o(mocks.netMock.request.callCount).equals(1)
			o(mocks.netMock.request.args).deepEquals([
				"some://url/file",
				{
					method: "GET",
					headers: {
						v: "foo",
						accessToken: "bar",
					},
					timeout: 20000,
				},
			])

			const expectedFilePath = "/tutanota/tmp/path/download/nativelyDownloadedFile"
			o(mocks.fsMock.createWriteStream.callCount).equals(1)
			o(mocks.fsMock.createWriteStream.args).deepEquals([expectedFilePath, {emitClose: true}])

			// the response arrives only after the request was finalized
			const request = mocks.netMock.Request.mockedInstances[0]
			request.callbacks["response"](response)

			const downloadResult = await promise
			o(downloadResult).deepEquals({
				statusCode: 200,
				errorId: null,
				precondition: null,
				suspensionTime: null,
				encryptedFileUri: expectedFilePath,
			})

			const ws = WriteStream.mockedInstances[0]
			o(response.pipe.callCount).equals(1)
			o(response.pipe.args[0]).deepEquals(ws)
			o(ws.on.callCount).equals(2)("finish and close handlers registered")
			o(ws.close.callCount).equals(1)("stream is closed")
		})

		o("404 error rejects with the status code", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			const response = new mocks.netMock.Response(404)
			response.headers["error-id"] = "123"

			const promise = dl.downloadNative("some://url/file", "nativelyDownloadedFile", {
				v: "foo",
				accessToken: "bar",
			})
			await delay(1)
			const request = mocks.netMock.Request.mockedInstances[0]
			request.callbacks["response"](response)

			const error = await assertThrows(Error, () => promise)
			o(error.message).equals("404")

			o(mocks.fsMock.createWriteStream.callCount).equals(1)("createStream calls")
			const ws = WriteStream.mockedInstances[0]
			o(ws.on.callCount).equals(2)("finish handler + fresh close handler")
			o(ws.removeAllListeners.callCount).equals(2)("close listeners discarded twice")
			o(ws.removeAllListeners.args[0]).equals("close")
			o(ws.close.callCount).equals(1)("stream is closed")
			o(mocks.fsMock.promises.unlink.callCount).equals(1)("partial file removed")
			o(mocks.fsMock.promises.unlink.args[0]).equals("/tutanota/tmp/path/download/nativelyDownloadedFile")
		})

		o("retry-after rejects with the status code", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			const response = new mocks.netMock.Response(TooManyRequestsError.CODE)
			response.headers["error-id"] = "123"
			response.headers["retry-after"] = "20"

			const promise = dl.downloadNative("some://url/file", "nativelyDownloadedFile", {
				v: "foo",
				accessToken: "bar",
			})
			await delay(1)
			const request = mocks.netMock.Request.mockedInstances[0]
			request.callbacks["response"](response)

			const error = await assertThrows(Error, () => promise)
			o(error.message).equals(String(TooManyRequestsError.CODE))
			o(mocks.fsMock.createWriteStream.callCount).equals(1)("createStream calls")
		})

		o("IO error during download", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			const response = new mocks.netMock.Response(200)
			// the response never completes, we error out instead
			response.pipe = o.spy((dest, opts) => response)
			const error = new Error("Test! I/O error")

			const promise = dl.downloadNative("some://url/file", "nativelyDownloadedFile", {
				v: "foo",
				accessToken: "bar",
			})
			await delay(1)
			const request = mocks.netMock.Request.mockedInstances[0]
			request.callbacks["response"](response)
			response.callbacks["error"](error)

			const returnedError = await assertThrows(Error, () => promise)
			o(returnedError).equals(error)

			o(mocks.fsMock.createWriteStream.callCount).equals(1)("createStream calls")
			const ws = WriteStream.mockedInstances[0]
			o(ws.close.callCount).equals(1)("stream is closed")
			o(mocks.fsMock.promises.unlink.calls.map(c => c.args)).deepEquals([
				["/tutanota/tmp/path/download/nativelyDownloadedFile"],
			])("unlink")
		})

		o("request error rejects", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			const error = new Error("request failed")

			const promise = dl.downloadNative("some://url/file", "nativelyDownloadedFile", {
				v: "foo",
				accessToken: "bar",
			})
			await delay(1)
			const request = mocks.netMock.Request.mockedInstances[0]
			request.callbacks["error"](error)

			const returnedError = await assertThrows(Error, () => promise)
			o(returnedError).equals(error)
			o(mocks.fsMock.createWriteStream.callCount).equals(1)("createStream calls")
			o(mocks.fsMock.promises.unlink.callCount).equals(1)("partial file removed")
		})
	})

	o.spec("open", function () {
		o("open", async function () {
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			return dl
				.open("/some/folder/file")
				.then(() => {
					o(mocks.electronMock.shell.openPath.callCount).equals(1)
					o(mocks.electronMock.shell.openPath.args.length).equals(1)
					o(mocks.electronMock.shell.openPath.args[0]).equals("/some/folder/file")
				})
				.then(() => dl.open("invalid"))
				.then(() => o(false).equals(true))
				.catch(() => {
					o(mocks.electronMock.shell.openPath.callCount).equals(2)
					o(mocks.electronMock.shell.openPath.args.length).equals(1)
					o(mocks.electronMock.shell.openPath.args[0]).equals("invalid")
				})
		})
		o("open on windows", async function () {
			n.setPlatform("win32")
			const mocks = standardMocks()
			const dl = makeMockedDownloadManager(mocks)
			await dl.open("exec.exe")
			o(mocks.electronMock.dialog.showMessageBox.callCount).equals(1)
			o(mocks.electronMock.shell.openPath.callCount).equals(0)
		})
	})
})