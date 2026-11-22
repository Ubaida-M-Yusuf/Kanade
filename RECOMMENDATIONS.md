# "For You" recommendations

Status: **built.** This describes how it works.

## What the user sees

* **For You** in the left sidebar ranks the library by how well each song fits the user's taste, with a match %
  on every card. Every normal filter works here (search, OP/ED, year, series, number, favourites and the Filters
  panel). The **Best match** sort is the default in this view.
* Three tabs: **For you** (songs not rated yet), **Liked** (likes and favourites) and **Hidden** (dislikes, each
  with an *Unhide* button).
* **👍 / 👎 on every card** (hover), in the details panel, in the right-click menu and on the **L** / **D** keys.
  A rating applies to the whole song (every version). A disliked song leaves For You at once; an **Undo** link
  is in the toast.
* **Sound / Sound + look + genre** switch: rank by how songs sound, or by sound plus the look of the video and the
  anime's genres and tags. The choice is remembered.
* **Song types:** every song is typed by its own sound as **Intense**, **Energetic**, **Mellow** or **Calm** (see
  below). *All my taste* shows the best matches of each type (two rows each, at most two songs per franchise) and
  then everything; a type chip shows all matches of that type. Types can be renamed.
* The details panel explains a match: the % by sound and by sound + look + genre, the song type, and which liked
  song it sounds and looks most like.
* New results never reshuffle the page being viewed: they are used the next time For You is opened (a *Show*
  link appears in the header). The scroll position is kept when nothing changed.
* **Settings ▸ For You · your taste:** any number of liked folders (any drive, any video format, read-only);
  favourites can count as likes. A one-time **Install For You engine** button sets up the analysis engine.

## How it learns

Everything works on **songs**: all versions, resolutions and creditless copies of one theme count once.

| Part | What it is |
|---|---|
| Music | MERT (`m-a-p/MERT-v1-95M`, pinned revision), 3 × 10 s windows at 25/50/75 %, mid + upper layers averaged |
| Style | CLAP music checkpoint (`laion/larger_clap_music`): genre / mood / vocals embedding of the same windows |
| Song type | AudioSet classifier (`MIT/ast-finetuned-audioset-10-10-0.4593`, pinned revision) on the same windows |
| Look | SigLIP 2 (`google/siglip2-base-patch16-224`), averaged over the six frames the thumbnail is chosen from (the same frames for liked files and library videos) |
| Story | the anime's AniList genres and tags (rarer ones weigh more), release year and format, OP vs ED |

1. **Centring.** Every embedding has the library's average subtracted before comparing. Raw music embeddings of
   any two openings look ~0.96 similar (they share one big common direction). Without centring, held-out
   favourites landed around the 31st percentile of the old ranking.
2. **Liked folders → library copies.** Liked folders often hold YouTube or fansub copies of openings that are also
   in the library. Kanade matches them by series name + OP/ED + number (`taste_match`, names only), then confirms
   by sound: the same recording scores 0.68–0.96, other songs of the same series ≤ 0.44, so 0.60 is the cut-off.
   The model then learns from the library copy, which sounds and looks like what it ranks. Liked files without a
   library copy still count through similarity.
3. **Learned model.** A small regularised logistic regression over all four parts: liked songs (likes,
   favourites, library copies of liked files) are positives; disliked songs count against as strongly as a like
   counts for; every other song counts weakly against (most of a big library isn't to one person's taste). It
   trains in about a second on the CPU.
4. **Similarity.** Closeness to the 3 nearest liked songs (keeps different tastes apart and works from the very
   first like) minus closeness to the nearest disliked song.
5. The score is a blend of 3 and 4, shown as a percentile within the library.

**Song types.** A song's type comes from its own sound, not from which liked songs it resembles. The AudioSet
classifier (trained on human-labelled clips) scores how much a song sounds like exciting or angry music, metal,
punk, rock, drums, versus tender or sad music, lullabies, piano, ambient. Energy = log(high / low); ≥ 1.5 Intense,
≥ 0 Energetic, ≥ −2.5 Mellow, else Calm. On well-known songs it puts 21 of 23 on the right side (Death Note OP2
+3.5, One Punch Man OP1 +3.7, Clannad ED1 −4.7, Violet Evergarden ED1 −4.6). Songs analysed before the classifier
was added get their energy predicted from their music embeddings by ridge regression trained on measured songs
(correlation 0.9 with the measured energy on held-out songs), matched to the measured spread. The types don't
affect the ranking; they only label songs and split the sections.

Learning runs after 5 new ratings, after 30 s without a new one, or when the user leaves For You, never on every
click. During a long analysis it happens inside the running engine; otherwise a short ranking run starts (a few
seconds, no re-analysis).

**Measured accuracy** on a real library (held-out liked songs, 5 folds grouped by series, so a series is never in
training and test at once; percentile among all songs not used for training, 100 = top):

| Ranking | Median | In the top 5 % | In the top 10 % |
|---|---|---|---|
| Previous version (raw similarity to liked files) | 31 | – | – |
| Sound only (learned) | 76–80 | 15–20 % | 29–34 % |
| Sound + look + genre (learned, default) | 94–95 | 47–49 % | 66–74 % |

Favourites and likes weigh 3× a liked-folder file: favourites are often newer than liked folders, and without the
extra weight held-out favourites ranked at a median of 71 instead of 81. Liking some of the top picks makes the
remaining top picks rise (tested: liking 10 of the top 30 moved all 20 others up; all stayed in the top 26).

## Performance

* The engine is a separate Python environment in `%LOCALAPPDATA%\Kanade\engine` (PyTorch + transformers + model
  weights), so the app itself stays small. It uses an NVIDIA GPU when present (CPU otherwise).
* **Each archive file is read once, for everything.** One sequential read covers the audio windows and the six
  frame positions; while the next file is read, five parallel workers take the frames, measure pace, prepare the
  CLAP spectrograms and write the upgraded thumbnail from the OS cache, and the GPU embeds the previous batch.
  Measured on a hard disk: ~0.45 s per video, limited by the disk (two or three readers at once are slower).
* Analysis order: the library copies of liked files and the rated / favourite videos first (For You is useful
  within minutes), then runs of 8 neighbouring files from each year folder in turn (every era early).
* Browsing never pauses the analysis; a video starting to play pauses new reads for a few seconds (15 s for VLC).
* Memory-safe: GPU memory capped at 30 % of the card and returned after every batch, small batches (retried
  smaller on a full GPU), and a hard per-process memory cap (8 GB; `KANADE_ENGINE_MEMLIMIT_GB`). Measured: about
  4.7 GB committed (GPU memory counts on Windows), 1.6 GB of RAM in use.
* Crash-safe: results are saved every 8 videos; on quit the app asks the engine to save and stop, and if the app
  disappears the engine finishes its batch and exits by itself. Only one engine runs at a time. New rankings
  replace the old ones in one transaction. Unreadable videos are retried next run.
* Incremental: after a library scan only new/changed videos are analysed; ratings and favourites only re-learn.

## Files

* `kanade/analyzer/kanade_analyzer.py`: the engine (reading, models, ranking run).
* `kanade/analyzer/taste_model.py`: the taste model (songs, centring, liked-file matching, learning, scores).
* `kanade/tastematch.py`: liked files → library candidates by name.
* `kanade/recommend.py`: engine install, process supervision, reading results, importing engine thumbnails.
* `features.db` tables: `feat` (vectors), `rec` (scores per video and song type), `taste_group`, `thumb_made`,
  `meta` (progress, last ranking summary). `library.db`: `rating`, `favorite`, `taste_match`.
