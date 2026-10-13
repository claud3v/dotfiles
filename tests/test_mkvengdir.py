"""Run with python3 -m unittest discover -s tests -v (zsh, jq, MKVToolNix)."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1] / '.functions'


def shell(script, *args, input=None):
    return subprocess.run(
        ['zsh', '-f', '-c', 'source "$1"; shift; ' + script, 'test',
         str(FUNCTIONS), *map(str, args)],
        input=input, text=True, capture_output=True,
    )


def track(track_id, kind='subtitles', codec='SubRip/SRT', **properties):
    return {'id': track_id, 'type': kind, 'codec': codec,
            'properties': {'language': 'eng', **properties}}


def choose(*subtitles, duration=3100, audio=None):
    data = {'container': {'properties': {'duration': duration * 10**9}},
            'tracks': [audio or track(1, 'audio'), *subtitles]}
    result = shell('_mkveng_select_tracks', input=json.dumps(data))
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class SelectionTests(unittest.TestCase):
    def test_forced_first_and_more_cues_in_sdh(self):
        result = choose(track(2, forced_track=True, tag_number_of_frames='37'),
                        track(3, tag_number_of_frames='819'),
                        track(4, flag_hearing_impaired=True, tag_number_of_frames='938'))
        self.assertEqual(result['subtitle']['id'], 3)

    def test_partial_and_commentary_names_without_flags(self):
        for name in ['Forced', 'Signs & Songs', 'Signs/Songs', 'Commentary']:
            with self.subTest(name=name):
                self.assertEqual(choose(track(2, track_name=name), track(3))['subtitle']['id'], 3)

    def test_sdh_fallback_preserves_label(self):
        for properties in [{'flag_hearing_impaired': True}, {'track_name': 'English CC'}]:
            result = choose(track(2, forced_track=True), track(3, **properties))
            self.assertEqual(result['subtitle']['id'], 3)
            self.assertEqual(result['subtitle_name'], 'English (SDH)')

    def test_only_forced_or_unknown_language_is_not_full_english(self):
        for properties in [{'forced_track': True}, {'language': 'und'}, {'language': 'fra'}]:
            self.assertIn('error', choose(track(2, **properties)))

    def test_ietf_english(self):
        self.assertEqual(choose(track(2, language='und', language_ietf='en-US'))['subtitle']['id'], 2)

    def test_ambiguous_full_tracks_require_manual_selection(self):
        self.assertIn('multiple', choose(track(2), track(3))['error'])

    def test_got_forced_and_image_tracks_lose_to_full_srt(self):
        result = choose(
            track(5, track_name='English-FORCED-SRT', forced_track=True,
                  tag_number_of_frames='7'),
            track(6, track_name='English-SRT', tag_number_of_frames='700'),
            track(7, codec='VobSub', tag_number_of_frames='7'),
            track(8, codec='VobSub', tag_number_of_frames='740'))
        self.assertEqual(result['subtitle']['id'], 6)
        self.assertIn('preferred text over image', result['reason'])

    def test_text_formats_preferred_over_images(self):
        for properties in [{'codec': 'SubRip/SRT'}, {'codec': 'SubStationAlpha'},
                           {'codec': 'WebVTT'},
                           {'codec': 'unknown', 'codec_id': 'S_TEXT/ASS'}]:
            with self.subTest(properties=properties):
                result = choose(track(2, codec='HDMV PGS'), track(3, **properties))
                self.assertEqual(result['subtitle']['id'], 3)

    def test_forced_or_sparse_text_does_not_beat_full_image(self):
        for properties in [{'forced_track': True}, {'tag_number_of_frames': '7'}]:
            with self.subTest(properties=properties):
                result = choose(track(2, **properties),
                                track(3, codec='VobSub', tag_number_of_frames='740'))
                self.assertEqual(result['subtitle']['id'], 3)

    def test_ordinary_image_still_preferred_over_sdh_text(self):
        result = choose(track(2, codec='VobSub'), track(3, flag_hearing_impaired=True))
        self.assertEqual(result['subtitle']['id'], 2)

    def test_ambiguous_tracks_in_preferred_format_still_rejected(self):
        self.assertIn('multiple', choose(track(2), track(3), track(4, codec='VobSub'))['error'])
        self.assertIn('multiple', choose(track(2, codec='VobSub'),
                                         track(3, codec='HDMV PGS'))['error'])

    def test_unmarked_sparse_track_loses_to_full_sdh(self):
        result = choose(track(2, tag_number_of_frames='37'),
                        track(3, flag_hearing_impaired=True, tag_number_of_frames='938'))
        self.assertEqual(result['subtitle']['id'], 3)

    def test_short_sparse_span_needs_inspection(self):
        self.assertIn('sparse', choose(track(2, tag_number_of_frames='30',
                                            tag_duration='00:02:00.000000000'))['error'])

    def test_missing_statistics_and_short_video_are_allowed(self):
        self.assertEqual(choose(track(2))['subtitle']['id'], 2)
        self.assertEqual(choose(track(2, tag_number_of_frames='2',
                                      tag_duration='00:00:01.000'), duration=5)['subtitle']['id'], 2)

    def test_unknown_audio_not_relabelled(self):
        self.assertIn('error', choose(track(2), audio=track(1, 'audio', language='und')))


class FileSafetyTests(unittest.TestCase):
    def test_tagged_files_and_existing_outputs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['Episode.mkv', 'Episode[ENG].mkv', 'Other[ENG].mkv']:
                (root / name).write_text(name)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            result = shell('mkvengdir "$1"', root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertIn('Skipping existing output', result.stdout)

    def test_failed_remux_is_not_published_and_temp_is_not_reprocessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mkv'
            source.write_bytes(b'original')
            metadata = json.dumps({'tracks': [track(1, 'audio'), track(2)]})
            script = r'''fixture_json="$2"
                mkvmerge() {
                  if [[ "$1" == -J ]]; then
                    printf '%s' "$fixture_json"
                  else
                    printf 'incomplete' > "$2"
                    return 2
                  fi
                }
                mkvengdir "$1"'''
            result = shell(script, root, metadata)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'source[ENG].mkv').exists())
            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(len(list(root.glob('.mkveng.*/output.mkv'))), 1)
            result = shell('mkvengdir --dry-run "$1"', root)
            self.assertNotIn('/output.mkv', result.stdout)

    def test_cleanup_preserves_unpaired_episode_and_refuses_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in [('one.mkv', b'old'), ('one[ENG].mkv', b'keep'),
                                  ('two.mkv', b'unpaired')]:
                (root / name).write_bytes(content)
            result = shell('cleanNonEngMkvs "$1"', root, input='y\n')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'one.mkv').read_bytes(), b'keep')
            self.assertEqual((root / 'two.mkv').read_bytes(), b'unpaired')
            result = shell('cleanNonEngMkvs "$1"', root, input='y\n')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(list(root.iterdir())), 2)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('mkvextract'), 'needs ffmpeg and mkvextract')
class RemuxTests(unittest.TestCase):
    def test_real_remux_selects_full_track_and_preserves_payloads(self):
        with tempfile.TemporaryDirectory(prefix='mkveng-test-') as tmp:
            root = Path(tmp)
            media = root / 'media'
            media.mkdir()
            base = root / 'base.mkv'
            source = media / 'Episode with spaces.mkv'
            forced = root / 'forced.srt'
            full = root / 'full.srt'
            forced.write_text('1\n00:00:00,200 --> 00:00:00,700\nA sign\n\n')
            full.write_text('1\n00:00:00,200 --> 00:00:00,700\nHello.\n\n'
                            '2\n00:00:00,800 --> 00:00:01,600\nGoodbye.\n\n')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=black:s=32x32:r=5:d=2', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=2', '-c:v', 'libx264', '-c:a', 'flac',
                            str(base)], check=True, capture_output=True)
            subprocess.run(['mkvmerge', '-o', str(source), '--language', '1:eng', str(base),
                            '--language', '0:eng', '--forced-track', '0:true', str(forced),
                            '--language', '0:eng', str(full)], check=True, capture_output=True)
            original = source.read_bytes()
            result = shell('mkvengdir --dry-run "$1"', media)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Subtitle track ID: 3', result.stdout)
            self.assertEqual(len(list(media.iterdir())), 1)
            result = shell('mkvengdir "$1"', media)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = media / 'Episode with spaces[ENG].mkv'
            self.assertEqual(source.read_bytes(), original)
            metadata = json.loads(subprocess.check_output(['mkvmerge', '-J', str(output)]))
            self.assertEqual(len(metadata['tracks']), 3)
            props = metadata['tracks'][2]['properties']
            self.assertEqual(props['track_name'], 'English')
            self.assertTrue(props['default_track'])
            self.assertFalse(props['forced_track'])
            for file, prefix, subtitle_id in [(source, 'before', 3), (output, 'after', 2)]:
                extracted = subprocess.run(['mkvextract', str(file), 'tracks',
                                f'0:{root / (prefix + ".video")}',
                                f'1:{root / (prefix + ".audio")}',
                                f'{subtitle_id}:{root / (prefix + ".srt")}'],
                               capture_output=True)
                self.assertEqual(extracted.returncode, 0, extracted.stdout.decode() + extracted.stderr.decode())
            for extension in ['video', 'audio', 'srt']:
                self.assertEqual((root / ('before.' + extension)).read_bytes(),
                                 (root / ('after.' + extension)).read_bytes())
            self.assertEqual(len(list(media.iterdir())), 2)
            before_rerun = output.read_bytes()
            result = shell('mkvengdir "$1"', media)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output.read_bytes(), before_rerun)
            self.assertEqual(len(list(media.iterdir())), 2)


if __name__ == '__main__':
    unittest.main()
