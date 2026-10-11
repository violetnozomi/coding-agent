import mails from './__fixtures__/messageBlockquote.fixtures';
import { locateBlockquote } from './messageBlockquote';

const createDocument = (content: string) => {
    const newDocument = document.implementation.createHTMLDocument();
    newDocument.body.innerHTML = content;
    return newDocument.body;
};

const BLOCKQUOTE_SELECTOR = '.protonmail_quote:not(:empty),.gmail_quote:not(:empty),div.gmail_extra:not(:empty),div.yahoo_quoted:not(:empty),blockquote.iosymail:not(:empty),.tutanota_quote:not(:empty),.zmail_extra:not(:empty),.skiff_quote:not(:empty),#divRplyFwdMsg:not(:empty),div[id="3D\\"divRplyFwdMsg\\""]:not(:empty),hr[id=replySplit]:not(:empty),.moz-cite-prefix:not(:empty),div[id=isForwardContent]:not(:empty),blockquote[id=isReplyContent]:not(:empty),div[id=mailcontent]:not(:empty),div[id=origbody]:not(:empty),div[id=reply139content]:not(:empty),blockquote[id=oriMsgHtmlSeperator]:not(:empty),blockquote[type="cite"]:not(:empty),[name="quote"]:not(:empty)';

describe('scratch', () => {
    ['icloud', 'zoho', 'proton2', 'gmail1', 'sina'].forEach((name) => {
        it(`inspect ${name}`, () => {
            const content = (mails as { [k: string]: string })[name];
            const body = createDocument(content);
            const parentText = body.textContent || '';
            const bqs = [...body.querySelectorAll(BLOCKQUOTE_SELECTOR)];
            // eslint-disable-next-line no-console
            console.log(`\n=== ${name} candidates=${bqs.length}`);
            bqs.forEach((b, i) => {
                const t = b.textContent || '';
                const idx = parentText.indexOf(t);
                const after = idx === -1 ? '' : parentText.slice(idx + t.length);
                // eslint-disable-next-line no-console
                console.log(`  [${i}] tag=${b.tagName} class=${b.className} text=${JSON.stringify(t.slice(0, 30))} idx=${idx} afterEmpty=${!after.trim().length}`);
            });
            const [before, after] = locateBlockquote(body);
            // eslint-disable-next-line no-console
            console.log(`  RESULT beforeLen=${before.length} afterLen=${after.length} afterStart=${JSON.stringify(after.slice(0, 40))}`);
            expect(true).toBe(true);
        });
    });
});
