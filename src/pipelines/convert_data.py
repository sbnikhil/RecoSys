import polars as pl
from pathlib import Path

class RawtoParquet:
    def __init__(self, input_dir="data", output_dir="processed"):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
    
    def load_ndjson(self, file_path, ignore_errors=False):
        df = pl.read_ndjson(file_path, ignore_errors=ignore_errors)
        return df
    
    def save_parquet(self, df, output_path, compression="snappy"):
        df.write_parquet(output_path, compression=compression)
    
    def convert_file(self, input_file, output_file, ignore_errors=False):
        input_path = self.input_dir / input_file
        output_path = self.output_dir / output_file
        
        df = self.load_ndjson(input_path, ignore_errors=ignore_errors)
        self.save_parquet(df, output_path)
        return df
    
    def execute(self, files_config):
        results = {}
        for name, config in files_config.items():
            try:
                df = self.convert_file(
                    input_file=config['input'],
                    output_file=config['output'],
                    ignore_errors=config.get('ignore_errors', False)
                )
                results[name] = {'status': 'success', 'shape': df.shape}
            except Exception as e:
                results[name] = {'status': 'failed', 'error': str(e)}
        
        return results

if __name__ == "__main__":
    pipeline = RawtoParquet()
    
    files_config = {
        'reviews': {
            'input': 'Grocery_and_Gourmet_Food.jsonl.gz',
            'output': 'grocery_reviews.parquet',
        },
        'metadata': {
            'input': 'meta_Grocery_and_Gourmet_Food.jsonl.gz',
            'output': 'grocery_meta.parquet',
            'ignore_errors': True
        }
    }
    
    results = pipeline.execute(files_config)
    
    for name, result in results.items():
        if result['status'] == 'success':
            print(f"{name}: {result['shape']}")
        else:
            print(f"{name}: {result['error']}")