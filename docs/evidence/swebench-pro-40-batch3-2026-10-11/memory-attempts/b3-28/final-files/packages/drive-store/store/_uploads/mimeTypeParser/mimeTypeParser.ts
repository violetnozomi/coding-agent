import { mimetypeFromExtension } from './helpers';

export async function mimeTypeFromFile(input: File) {
    if (input.type) {
        return input.type;
    }

    return (await mimetypeFromExtension(input.name)) || 'application/octet-stream';
}
