"""whisper_model_pins.py -- the Whisper models this pipeline may load, pinned.

GENERATED from the Hugging Face API tree of each repo at the pinned commit
(2026-09-25). Every file carries its size and either the SHA-256 Hugging Face
records for LFS objects or the git blob SHA-1 for small files, so a download is
checked against what was reviewed, not against whatever the repo or a CDN
serves today. This is a .py file on purpose: the integrity monitor hashes .py,
so an edited pin is a drift finding.

Changing a pin is a maintainer act with review, like installers/plugin-pins.json:
read the repo diff between the old and new commit, then regenerate this file.
"""

PINS = {
    'mlx-community/whisper-large-v3-turbo': {
        # macOS Apple Silicon default (mlx-whisper). Weights unchanged since 2024-11-01; the newest commit only added a metadata tag.
        "revision": 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb',  # 2026-04-12
        "files": {
            '.gitattributes': {'size': 1519, 'git_sha1': 'a6344aac8c09253b3b630fb776ae94478aa0275b'},
            'README.md': {'size': 359, 'git_sha1': '3569b423420febc3a0e1949b9e6aff2bf3bba2c7'},
            'config.json': {'size': 268, 'git_sha1': '6ac9a52a28f70a2e5681c250a470eca6e9c8cc3e'},
            'weights.safetensors': {'size': 1613977612, 'sha256': '951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6'},
        },
    },
    'onnx-community/whisper-small': {
        # Windows ARM64 default (onnxruntime). The newest commit only added decoder_model_merged_fp16.onnx, which the default variant does not load.
        "revision": '36050c46d777d46dc4b5f43f6d90574fc38f8732',  # 2025-06-19
        "files": {
            '.gitattributes': {'size': 1519, 'git_sha1': 'a6344aac8c09253b3b630fb776ae94478aa0275b'},
            'README.md': {'size': 533, 'git_sha1': '4dc6ce2168aaa7f688f0f5b17d49b1e66d89f815'},
            'added_tokens.json': {'size': 34604, 'git_sha1': 'e3d256c988462aa153dcabe2aa38b8e9b436c06f'},
            'config.json': {'size': 2227, 'git_sha1': 'b0447377b1ade057f991a6a0870d2a91de762f7f'},
            'generation_config.json': {'size': 3893, 'git_sha1': '703dc78ee83e87fca3ace72170253cdfe4cd2d13'},
            'merges.txt': {'size': 493869, 'git_sha1': '6038932a2a1f09a66991b1c2adae0d14066fa29e'},
            'normalizer.json': {'size': 52666, 'git_sha1': 'dd6ae819ad738ac1a546e9f9282ef325c33b9ea0'},
            'onnx/decoder_model.onnx': {'size': 614865004, 'sha256': '12130ce1e82372a8e54e753d3fb4339f289470cbbe2eceb9c8bf89cb2cc6fe63'},
            'onnx/decoder_model_bnb4.onnx': {'size': 225599806, 'sha256': '683a68c0951833eb7fb5b38319e9acf41a01ba63681584f3245ac906859ecd93'},
            'onnx/decoder_model_fp16.onnx': {'size': 307924740, 'sha256': 'bbf06f4be0e38b76d6cf107da618a554f311c406a873866000e23383131e3ba5'},
            'onnx/decoder_model_int8.onnx': {'size': 155989346, 'sha256': '0486afa8eb464bf2210d07aca0cb3f5d91d3f2f01cbe47f9f1e6ff9122abaaf6'},
            'onnx/decoder_model_merged.onnx': {'size': 615324301, 'sha256': '6ed5e35feaba79ad2e89b368ddc7b4ddaa3c00b4c37a664375d3428a76fecc6a'},
            'onnx/decoder_model_merged_bnb4.onnx': {'size': 226073167, 'sha256': '783b50b5a9f30f6c1f45e7d695ed40af55c14f6a6b395c7a30002598ac4217fd'},
            'onnx/decoder_model_merged_fp16.onnx': {'size': 308583076, 'sha256': '22aba6c7f5193701cbe1519051b6ef097eb530ad6887b7093065ec59b830f61d'},
            'onnx/decoder_model_merged_int8.onnx': {'size': 156750845, 'sha256': 'ec07c3cbb64172c39791e26ee870a65ac22b458c36722bfe2776b3dbf741e0c9'},
            'onnx/decoder_model_merged_q4.onnx': {'size': 233149327, 'sha256': '795c61b344576719c2b249cc9911c78b4cc552c8c6a5407be21a3380a0791b13'},
            'onnx/decoder_model_merged_quantized.onnx': {'size': 156750845, 'sha256': 'ec07c3cbb64172c39791e26ee870a65ac22b458c36722bfe2776b3dbf741e0c9'},
            'onnx/decoder_model_merged_uint8.onnx': {'size': 156750906, 'sha256': 'b92188d1a3c4aea369131893f9649228b8605128e9a8a86e5e4da6ebb61b8f87'},
            'onnx/decoder_model_q4.onnx': {'size': 232676830, 'sha256': 'f9df829aeac7345c9b08c95cdaf8fdf9af82bba9e446c75f840889000afa124c'},
            'onnx/decoder_model_quantized.onnx': {'size': 155989346, 'sha256': '0486afa8eb464bf2210d07aca0cb3f5d91d3f2f01cbe47f9f1e6ff9122abaaf6'},
            'onnx/decoder_model_uint8.onnx': {'size': 155989407, 'sha256': '1bc30481d9fa4e9644a2878f9816857d78f28c918c41db8054496f1181e85e0e'},
            'onnx/decoder_with_past_model.onnx': {'size': 558117914, 'sha256': '8e17aa98d76cd503552b39b6a716bc51d896f61aeab022f8cfbaf9aa6540e72a'},
            'onnx/decoder_with_past_model_bnb4.onnx': {'size': 217509476, 'sha256': 'c3a300e2757c5c3b0259ec808077008bba830769cfe8f45954790211bbf15d7d'},
            'onnx/decoder_with_past_model_fp16.onnx': {'size': 279478163, 'sha256': '3a65cf85fdb5f77bda73bc22c360bc1de72b226f6c63dc3b0a0be17db640ac86'},
            'onnx/decoder_with_past_model_int8.onnx': {'size': 141652708, 'sha256': 'f1eb4ac7fe2b3aeed93a8de8f478af45b8fcef7302491ffcf9c84f33f71b3e7b'},
            'onnx/decoder_with_past_model_q4.onnx': {'size': 223701932, 'sha256': 'd4ebca1f524757f3ebea0591412ec15889acf7384c2830f039a824951d765a17'},
            'onnx/decoder_with_past_model_quantized.onnx': {'size': 141652708, 'sha256': 'f1eb4ac7fe2b3aeed93a8de8f478af45b8fcef7302491ffcf9c84f33f71b3e7b'},
            'onnx/decoder_with_past_model_uint8.onnx': {'size': 141652756, 'sha256': 'f3b45f22b0b35565800c4f47eb6520d9e524d6a440872d6da44e58c2c1aafaf8'},
            'onnx/encoder_model.onnx': {'size': 352825870, 'sha256': 'b37cd6625dc36f9178ec7539a1876b9680ea26a910097e092be39dc766320c7b'},
            'onnx/encoder_model_bnb4.onnx': {'size': 60874216, 'sha256': '5ed12f343f004e5060719d2cf892b5c5e801e89919c1649f55bea78227c84040'},
            'onnx/encoder_model_fp16.onnx': {'size': 176607756, 'sha256': '5549cd8666ff4b694ceb128bfa48b95bdcceec29075cf2c2212f90002cc058de'},
            'onnx/encoder_model_int8.onnx': {'size': 92326127, 'sha256': '2601c9eb2d345c5916d4576d36f663a7c96589740fb2273828c48c3fc2c7db75'},
            'onnx/encoder_model_q4.onnx': {'size': 66182104, 'sha256': '9f088ad0fe15ba2cf094c9fb22ac7fe1111fc9ef317c1e4d1f6a3bca38a931ba'},
            'onnx/encoder_model_quantized.onnx': {'size': 92326160, 'sha256': 'a43a83f3c5361cd591cfa7c36f14b43cf7cb22f47a415cc14a8d557be800fa92'},
            'onnx/encoder_model_uint8.onnx': {'size': 92326160, 'sha256': 'a43a83f3c5361cd591cfa7c36f14b43cf7cb22f47a415cc14a8d557be800fa92'},
            'preprocessor_config.json': {'size': 339, 'git_sha1': '91876762a536a746d268353c5cba57286e76b058'},
            'quantize_config.json': {'size': 10126, 'git_sha1': 'dce20a7a4c0260823a4ee78104e723ebbe1b474b'},
            'special_tokens_map.json': {'size': 2194, 'git_sha1': 'bf69932dca4b3719b59fdd8f6cc1978109509f6c'},
            'tokenizer.json': {'size': 2480466, 'git_sha1': '1e95340ff836fad1b5932e800fb7b8c5e6d78a74'},
            'tokenizer_config.json': {'size': 282683, 'git_sha1': 'd13b786c04765fb1a06492b53587752cd67665ea'},
            'vocab.json': {'size': 1036584, 'git_sha1': '90e797dd4fd05d9dea443d702ca06be2463c5f2f'},
        },
    },
    'Systran/faster-whisper-base': {
        # x86 CPU default (faster-whisper). Neither maintainer machine uses it; pinned so the fallback is not unpinned.
        "revision": 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66',  # 2023-11-23
        "files": {
            '.gitattributes': {'size': 1477, 'git_sha1': 'c7d9f3332a950355d5a77d85000f05e6f45435ea'},
            'README.md': {'size': 1991, 'git_sha1': 'cddfec65ffb3e3df45d2234f09f3d261bf9a9c0a'},
            'config.json': {'size': 2309, 'git_sha1': '867cf1a0fece1394e01d55e287ba2f09a577c046'},
            'model.bin': {'size': 145217532, 'sha256': 'd01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9'},
            'tokenizer.json': {'size': 2203239, 'git_sha1': '7818adb6de9fa3064d3ff81226fdd675be1f6344'},
            'vocabulary.txt': {'size': 459861, 'git_sha1': 'c9074644d9d1205686f16d411564729461324b75'},
        },
    },
}
