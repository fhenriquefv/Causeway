from gflags import FLAGS, DEFINE_integer
import io
from os import path
import shutil
import sys

from data.io import CausalityStandoffReader, CausalityStandoffWriter
from util.streams import CharacterTrackingStreamWrapper


DEFINE_integer('start_sentence', 0, 'Sentence at which to start copying')
DEFINE_integer('end_sentence', -1, 'Sentence at which to stop copying')


if __name__ == '__main__':
    argv = sys.argv
    argv = FLAGS(argv)
    in_file, out_directory = argv[1:]

    reader = CausalityStandoffReader(in_file)
    doc = reader.get_next()

    base_name = path.splitext(path.split(in_file)[1])[0]
    in_txt_name = path.splitext(in_file)[0] + '.txt'
    out_txt_name = path.join(out_directory, base_name + '.txt')

    if FLAGS.start_sentence > 0:
        start_copying_char = doc.sentences[
            FLAGS.start_sentence].document_char_offset
    else:
        start_copying_char = 0

    with CharacterTrackingStreamWrapper(
        io.open(in_txt_name, 'rb'), FLAGS.reader_codec) as in_txt_file:
        with io.open(out_txt_name, 'wb') as out_txt_file:
            while in_txt_file.character_position < start_copying_char:
                in_txt_file.read(1)
            shutil.copyfileobj(in_txt_file, out_txt_file)

    out_ann_name = path.join(out_directory, base_name + '.ann')
    writer = CausalityStandoffWriter(out_ann_name, start_copying_char)

    def instances_getter(document):
        if FLAGS.end_sentence == -1:
            sentences_to_write = document.sentences[FLAGS.start_sentence:]
        else:
            sentences_to_write = document.sentences[FLAGS.start_sentence:
                                                    FLAGS.end_sentence]
        return sentences_to_write
    writer.write_all_instances(doc, instances_getter)