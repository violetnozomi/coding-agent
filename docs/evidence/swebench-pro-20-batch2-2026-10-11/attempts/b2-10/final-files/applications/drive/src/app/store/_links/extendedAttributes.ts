import { CryptoProxy, PrivateKeyReference, PublicKeyReference, VERIFICATION_STATUS } from '@proton/crypto';
import { FILE_CHUNK_SIZE } from '@proton/shared/lib/drive/constants';
import { decryptSigned } from '@proton/shared/lib/keys/driveKeys';

/**
 * Recursively makes every property of a type optional, leaving primitives and
 * arrays untouched. This is used to describe inputs which may only be partially
 * known, such as serialized extended attributes where any field can be missing.
 */
export type DeepPartial<T> = T extends object
    ? T extends Array<infer U>
        ? Array<DeepPartial<U>>
        : { [P in keyof T]?: DeepPartial<T[P]> }
    : T;

export type CanonicalDigestName = 'SHA1';

export type Digests = { [Name in CanonicalDigestName]?: string };

export interface ExtendedAttributes {
    Common: {
        ModificationTime?: string;
        Size?: number;
        BlockSizes?: number[];
        Digests?: Digests;
    };
    Media?: {
        Width: number;
        Height: number;
    };
}

export interface ParsedExtendedAttributes {
    Common: {
        ModificationTime?: number;
        Size?: number;
        BlockSizes?: number[];
        Digests?: Digests;
    };
    Media?: {
        Width: number;
        Height: number;
    };
}

/**
 * Extended attributes can come from an untrusted or incomplete source, so any
 * part of the structure may be missing or malformed.
 */
export type MaybeExtendedAttributes = DeepPartial<ExtendedAttributes>;

export interface XAttrCreateParams {
    file: File;
    digests?: {
        sha1: string;
    };
    media?: {
        width: number;
        height: number;
    };
}

const CANONICAL_DIGEST_NAMES: CanonicalDigestName[] = ['SHA1'];

function toCanonicalDigestName(name: string): CanonicalDigestName | undefined {
    const upperCasedName = name.toUpperCase();
    return CANONICAL_DIGEST_NAMES.find((canonicalName) => canonicalName === upperCasedName);
}

function normalizeDigests(digests: XAttrCreateParams['digests']): Digests | undefined {
    if (digests === undefined) {
        return undefined;
    }
    const normalized: Digests = {};
    let hasDigest = false;
    for (const [name, value] of Object.entries(digests)) {
        const canonicalName = toCanonicalDigestName(name);
        if (canonicalName === undefined || typeof value !== 'string') {
            continue;
        }
        normalized[canonicalName] = value;
        hasDigest = true;
    }
    return hasDigest ? normalized : undefined;
}

export async function encryptFolderExtendedAttributes(
    modificationTime: Date,
    nodePrivateKey: PrivateKeyReference,
    addressPrivateKey: PrivateKeyReference
) {
    const xattr = createFolderExtendedAttributes(modificationTime);
    return encryptExtendedAttributes(xattr, nodePrivateKey, addressPrivateKey);
}

export function createFolderExtendedAttributes(modificationTime: Date): ExtendedAttributes {
    return {
        Common: {
            ModificationTime: dateToIsoString(modificationTime),
        },
    };
}

export async function encryptFileExtendedAttributes(
    file: File,
    nodePrivateKey: PrivateKeyReference,
    addressPrivateKey: PrivateKeyReference,
    media?: XAttrCreateParams['media'],
    digests?: XAttrCreateParams['digests']
) {
    const xattr = createFileExtendedAttributes({ file, media, digests });
    return encryptExtendedAttributes(xattr, nodePrivateKey, addressPrivateKey);
}

export function createFileExtendedAttributes({ file, media, digests }: XAttrCreateParams): ExtendedAttributes {
    const blockSizes = new Array(Math.floor(file.size / FILE_CHUNK_SIZE));
    blockSizes.fill(FILE_CHUNK_SIZE);
    blockSizes.push(file.size % FILE_CHUNK_SIZE);

    return {
        Common: {
            ModificationTime: dateToIsoString(new Date(file.lastModified)),
            Size: file.size,
            BlockSizes: blockSizes,
            Digests: normalizeDigests(digests),
        },
        Media: media
            ? {
                  Width: media.width,
                  Height: media.height,
              }
            : undefined,
    };
}

async function encryptExtendedAttributes(
    xattr: ExtendedAttributes,
    nodePrivateKey: PrivateKeyReference,
    addressPrivateKey: PrivateKeyReference
) {
    const xattrString = JSON.stringify(xattr);
    const { message } = await CryptoProxy.encryptMessage({
        textData: xattrString,
        encryptionKeys: nodePrivateKey,
        signingKeys: addressPrivateKey,
        compress: true,
    });
    return message;
}

export async function decryptExtendedAttributes(
    encryptedXAttr: string,
    nodePrivateKey: PrivateKeyReference,
    addressPublicKey: PublicKeyReference | PublicKeyReference[]
): Promise<{ xattrs: ParsedExtendedAttributes; verified: VERIFICATION_STATUS }> {
    const { data: xattrString, verified } = await decryptSigned({
        armoredMessage: encryptedXAttr,
        privateKey: nodePrivateKey,
        publicKey: addressPublicKey,
    });
    return {
        xattrs: parseExtendedAttributes(xattrString),
        verified,
    };
}

export function parseExtendedAttributes(xattrString: string): ParsedExtendedAttributes {
    let xattr: MaybeExtendedAttributes = {};
    try {
        const parsed = JSON.parse(xattrString);
        if (parsed !== null && typeof parsed === 'object') {
            xattr = parsed;
        } else {
            console.warn(`XAttr "${xattrString}" is not a JSON object`);
        }
    } catch (err) {
        console.warn(`XAttr "${xattrString}" is not valid JSON`);
    }
    return {
        Common: {
            ModificationTime: parseModificationTime(xattr),
            Size: parseSize(xattr),
            BlockSizes: parseBlockSizes(xattr),
            Digests: parseDigests(xattr),
        },
        Media: parseMedia(xattr),
    };
}

function parseModificationTime(xattr: MaybeExtendedAttributes): number | undefined {
    const modificationTime = xattr.Common?.ModificationTime;
    if (modificationTime === undefined) {
        return undefined;
    }
    const modificationDate = new Date(modificationTime);
    if (Number.isNaN(modificationDate.getTime())) {
        console.warn(`XAttr modification time "${modificationTime}" is not valid`);
        return undefined;
    }
    const modificationTimestamp = Math.trunc(modificationDate.getTime() / 1000);
    if (Number.isNaN(modificationTimestamp)) {
        console.warn(`XAttr modification time "${modificationTime}" is not valid`);
        return undefined;
    }
    return modificationTimestamp;
}

function parseSize(xattr: MaybeExtendedAttributes): number | undefined {
    const size = xattr.Common?.Size;
    if (size === undefined) {
        return undefined;
    }
    if (typeof size !== 'number') {
        console.warn(`XAttr file size "${size}" is not valid`);
        return undefined;
    }
    return size;
}

function parseBlockSizes(xattr: MaybeExtendedAttributes): number[] | undefined {
    const blockSizes = xattr.Common?.BlockSizes;
    if (blockSizes === undefined) {
        return undefined;
    }
    if (!Array.isArray(blockSizes)) {
        console.warn(`XAttr block sizes "${blockSizes}" is not valid`);
        return undefined;
    }
    if (!blockSizes.every((item) => typeof item === 'number')) {
        console.warn(`XAttr block sizes "${blockSizes}" is not valid`);
        return undefined;
    }
    return blockSizes;
}

function parseMedia(xattr: MaybeExtendedAttributes): { Width: number; Height: number } | undefined {
    const media = xattr.Media;
    if (media === undefined || media.Width === undefined || media.Height === undefined) {
        return undefined;
    }
    const width = media.Width;
    if (typeof width !== 'number') {
        console.warn(`XAttr media width "${width}" is not valid`);
        return undefined;
    }
    const height = media.Height;
    if (typeof height !== 'number') {
        console.warn(`XAttr media height "${height}" is not valid`);
        return undefined;
    }
    return {
        Width: width,
        Height: height,
    };
}

function parseDigests(xattr: MaybeExtendedAttributes): Digests | undefined {
    const digests = xattr.Common?.Digests;
    if (digests === undefined) {
        return undefined;
    }

    const parsed: Digests = {};
    let hasDigest = false;
    for (const [name, value] of Object.entries(digests)) {
        const canonicalName = toCanonicalDigestName(name);
        if (canonicalName === undefined) {
            console.warn(`XAttr digest "${name}" is not known`);
            continue;
        }
        if (typeof value !== 'string') {
            console.warn(`XAttr digest ${canonicalName} "${value}" is not valid`);
            continue;
        }
        parsed[canonicalName] = value;
        hasDigest = true;
    }

    return hasDigest ? parsed : undefined;
}

function dateToIsoString(date: Date) {
    const isDateValid = !Number.isNaN(date.getTime());
    return isDateValid ? date.toISOString() : undefined;
}
